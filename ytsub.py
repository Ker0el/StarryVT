#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 yt-dlp 下载的 YouTube 字幕（.vtt）转成引擎能直接用的整句 SRT。

为什么要这么绕：引擎的四个阶段是**按产物文件存在就跳过**的，而第四阶段
「原始字幕」的产物就是 `<输出名>-raw.srt`。把这个文件提前写好，引擎就会
跳过人声分离和语音识别，直接拿 YouTube 的字幕去纠错翻译——创作者自己写的
文字稿，专有名词零错误（实测同段 UE 教程：人工轨 Unreal/C++/Blueprint 全对，
本地 Parakeet 把 C++ 吃掉了、Unreal 拼成 Unrull）。

两种轨道的差别（都实测过）：
  人工/同步轨  一句一条，5~7 秒，句中标点齐全，条尾是 &nbsp; 填充
  YouTube 自动轨 1.1 秒一条滚动式，每句重复 2~3 次，几乎没有标点
所以自动轨必须先按「上一条是下一条的前缀」去重，再靠停顿断句。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

#: VTT 时间轴 `00:00:05.729 --> 00:00:08.270 align:start position:0%`
_RE_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)
#: 自动轨的词级标记 `<00:00:03.880><c> beginner's</c>`
_RE_WORD = re.compile(r"<(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})><c>([^<]*)</c>")
_RE_TAG = re.compile(r"<[^>]*>")
#: 自动轨里每句结尾常挂一个「正在说话」的光标标记
_RE_CUE_MARK = re.compile(r"\[(?:音乐|掌声|Music|Applause|__.*?__)\]", re.I)
#: 一句话说完的标点
_RE_SENT_END = re.compile(r"[.!?。！？][\"'”’)\]]*$")
#: cue 内部的句界：句号后跟空格再跟大写字母（别把 `U.S.` `3.5` 切了）
_RE_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")

#: 一条字幕最多多少字符（英文），超了就找最近的逗号断——引擎按条纠错，太长会稀释
MAX_CHARS = 120
#: 一条字幕最多多少秒，超了强制断句
MAX_SECONDS = 14.0
#: 两句之间静音超过这个秒数就当一句说完（自动轨没有标点时全靠它）
SENTENCE_GAP = 1.1
#: 自动轨里词与词之间超过这个间隔，认为是一句话的停顿
WORD_GAP = 0.7


# ---------------------------------------------------------------- 解析

def _sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _sec_short(stamp: str) -> float:
    h, m, rest = stamp.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _clean(text: str) -> str:
    text = _RE_TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", text).strip()


def read_blocks(path: Path) -> list[tuple[float, float, list[str]]]:
    """把 vtt 拆成 [(开始, 结束, 正文行)]，正文行原样保留（自动轨要靠标记判断）

    ⚠ 块的结束只认**真正空行**：YouTube 自动轨的正文里有一行是「一个空格」的填充，
    用 `strip()` 判断会把这一行当成分隔符，整块正文就没了——开头几个词就是这么丢的。
    """
    blocks: list[tuple[float, float, list[str]]] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        m = _RE_TIME.search(lines[i])
        if not m:
            i += 1
            continue
        start, end = _sec(*m.group(1, 2, 3, 4)), _sec(*m.group(5, 6, 7, 8))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].rstrip("\r") != "":
            body.append(lines[i])
            i += 1
        blocks.append((start, end, body))
    return blocks


def auto_words(blocks: list[tuple[float, float, list[str]]]) -> list[tuple[float, str]]:
    """自动轨：重建带时间戳的词流

    这种字幕每条 cue 的正文是「上一句的尾巴（无标记）+ 新加的词（带 <时间><c>词</c> 标记）」，
    还有一批只持续 0.01 秒的闪烁条。**只取带标记的那些行**，尾巴和闪烁条自然被排除，
    拼出来就是「每个词说出口的时刻」，比按 cue 去重干净得多。
    """
    words: list[tuple[float, str]] = []
    for start, _end, body in blocks:
        for line in body:
            if "<c>" not in line:
                continue                          # 上一句的尾巴 / 闪烁条
            first = _RE_WORD.search(line)
            assert first is not None
            lead = _clean(line[: first.start()])
            if lead:
                words.append((start, lead))
            for stamp, word in _RE_WORD.findall(line):
                text = _clean(word)
                if text:
                    words.append((_sec_short(stamp), text))
    # 滚动字幕偶尔会把同一个词写两遍（尾巴行也带标记时），压掉紧挨着的重复
    out: list[tuple[float, str]] = []
    for t, w in words:
        if out and out[-1][1] == w and t - out[-1][0] < 0.6:
            continue
        out.append((t, w))
    return out


def parse_vtt(path: Path) -> list[tuple[float, float, str]]:
    """读成 [(开始, 结束, 文本)]（人工轨用；自动轨走 auto_words）"""
    cues: list[tuple[float, float, str]] = []
    for start, end, body in read_blocks(path):
        text = _clean(" ".join(body))
        if text:
            cues.append((start, end, text))
    return cues


def _soft_cut(buf: list[tuple[float, str]]) -> int:
    """字数超限时回看一个「断在这儿不难看」的位置：逗号、分号、连接词。

    找不到就返回 0（照旧硬切）。只认后半段里的候选，免得切出一两个词的小碎片。
    """
    floor = len(buf) // 2
    for j in range(len(buf) - 1, floor, -1):
        word = buf[j - 1][1]
        if word.endswith((",", ";", ":")) or word.lower() in (
            "and", "but", "or", "so", "because", "which", "that", "when", "then",
        ):
            return j
    return 0


def group_words(words: list[tuple[float, str]]) -> list[tuple[float, float, str]]:
    """词流攒成整句：停顿、句末标点、长度上限，三者任一命中就断"""
    out: list[tuple[float, float, str]] = []
    buf: list[tuple[float, str]] = []
    start = end = 0.0
    i = 0
    while i < len(words):
        t, w = words[i]
        if not buf:
            start = t
        buf.append((t, w))
        end = t
        text = " ".join(x[1] for x in buf)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = nxt is None or bool(_RE_SENT_END.search(w))
        hard = ends or nxt[0] - t > WORD_GAP or (t - start) >= MAX_SECONDS
        if not hard and len(text) < MAX_CHARS:
            i += 1
            continue
        if not ends:
            cut = _soft_cut(buf)
            if cut and len(buf) - cut >= 2:          # 逗号处断开，剩下的接着攒
                out.append((start, min(buf[cut - 1][0] + 0.35, buf[cut][0]),
                            " ".join(x[1] for x in buf[:cut])))
                buf = buf[cut:]
                start = buf[0][0]
                i += 1
                continue
        out.append((start, min(end + 0.35, nxt[0]) if nxt else end + 0.35, text))
        buf = []
        i += 1
    if buf:
        out.append((start, buf[-1][0] + 0.35, " ".join(x[1] for x in buf)))
    return _fix_overlaps(out)


def _fix_overlaps(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """后一条压到前一条身上时，把前一条收在后者开口之前（差 10 毫秒）

    字幕叠着显示在播放器里会闪，配音按时间轴切段时也会读重。
    """
    out: list[tuple[float, float, str]] = []
    for i, (s, e, text) in enumerate(cues):
        if out and i + 1 <= len(cues):
            ns = cues[i + 1][0] if i + 1 < len(cues) else None
            prev_s, prev_e, prev_t = out[-1]
            if ns is not None and prev_e > ns - 0.01:
                out[-1] = (prev_s, max(prev_s + 0.05, ns - 0.01), prev_t)
        out.append((s, e, text))
    return out


def split_cues(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """人工轨：cue 里可能装着「半句 + 整句」——先把 cue 内部的句界切开

    时间按字符数摊给切出来的每段，够准了（人工轨本来就是一句一条对齐的）。
    """
    out: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        parts = [p.strip() for p in _RE_SENT_SPLIT.split(text) if p.strip()]
        if len(parts) == 1:
            out.append((start, end, text))
            continue
        span, at = end - start, start
        total = sum(len(p) for p in parts)
        for p in parts:
            share = (end - start) * len(p) / total if total else span
            out.append((round(at, 3), round(at + share, 3), p))
            at += share
    return out


def dedupe_rolling(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """兜底去重：后一条把前一条整句又抄了一遍时，只留新增的部分"""
    out: list[tuple[float, float, str]] = []
    prev = ""
    for start, end, text in cues:
        if text == prev:
            continue
        if prev and text.startswith(prev):
            fresh = text[len(prev):].strip()
            if fresh:
                out.append((start, end, fresh))
        else:
            out.append((start, end, text))
        prev = text
    return out


# ---------------------------------------------------------------- 断句

def _ends_sentence(text: str) -> bool:
    return bool(_RE_SENT_END.search(text))


def group_sentences(
    cues: list[tuple[float, float, str]],
    *,
    max_chars: int = MAX_CHARS,
    max_seconds: float = MAX_SECONDS,
    gap: float = SENTENCE_GAP,
) -> list[tuple[float, float, str]]:
    """把零碎的 cue 攒成整句。断句信号（任一命中就断）：

    * 句末标点（人工轨主要靠这个）
    * 与下一条之间的停顿超过 `gap` 秒
    * 攒到 `max_chars` 字符或 `max_seconds` 秒（兜底，防止一句没完没了）
    """
    out: list[tuple[float, float, str]] = []
    buf: list[str] = []
    start = end = 0.0
    for i, (s, e, text) in enumerate(cues):
        if not buf:
            start = s
        buf.append(text)
        end = e
        joined = " ".join(buf)
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        silence = nxt is not None and nxt[0] - e > gap
        done = (
            _ends_sentence(text)
            or silence
            or len(joined) >= max_chars
            or (end - start) >= max_seconds
            or nxt is None
        )
        if done:
            out.append((start, end, joined))
            buf = []
    return _fix_overlaps(out)


def wrap(text: str, width: int = 56) -> str:
    """按词折行成两行以内（字幕惯例：一条最多两行）"""
    if len(text) <= width:
        return text
    words, line, lines = text.split(), "", []
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    if len(lines) > 2:                                   # 超两行就重新均分
        half = len(text) // 2
        cut = text.rfind(" ", 0, half + 12) or half
        return text[:cut].strip() + "\n" + text[cut:].strip()
    return "\n".join(lines)


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[tuple[float, float, str]]) -> str:
    blocks = []
    for n, (s, e, text) in enumerate(cues, 1):
        if e <= s:
            e = s + 0.8
        # 字幕尽量保持一行：不在这里插换行，太长由 srt2ass 按画面宽度自动缩小字号
        one = re.sub(r"\s+", " ", text).strip()
        blocks.append(f"{n}\n{_ts(s)} --> {_ts(e)}\n{one}\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------- 对外

def find_vtt(video: Path, lang: str = "") -> Path | None:
    """找下载器放在视频旁边的字幕文件，优先人工轨

    yt-dlp 同时下人工轨和自动轨时，两边**文件名一模一样**（`<名字>.<语言>.vtt`），
    人工轨存在时它只写人工轨；只有自动轨时写自动轨。所以「找得到就是最好的那份」。
    """
    stem = video.stem
    cands = [p for p in video.parent.glob(f"{glob_escape(stem)}.*.vtt")
             if p.is_file()]
    if not cands:
        return None
    if lang:
        for p in cands:                                  # 语言对得上优先
            if p.name.split(".")[-2].lower().startswith(lang.lower()[:2]):
                return p
    return cands[0]


def glob_escape(name: str) -> str:
    """视频名里带 [] 等通配符时 glob 会当模式解析，转义掉"""
    return re.sub(r"([\[\]*?])", r"[\1]", name)


def build_srt(video: Path, lang: str = "") -> tuple[str, str] | None:
    """返回 (SRT 文本, 来源说明)，没字幕返回 None"""
    got = build(video, lang)
    return (got[0], got[2]) if got else None


def build(video: Path, lang: str = "") -> tuple[str, str, str] | None:
    """返回 (SRT 文本, stable.json 文本, 来源说明)，没字幕返回 None

    stable.json 是引擎「稳定化」阶段的产物。给它是为了让引擎跳过前面三步：
    引擎判断 `aligned_required = 没有 stable.json`，语音识别不需要时**连人声分离一起跳过**
    （见引擎 stages.py 的 _run_vocal_stage）。内容按真实结构从字幕推导，不是空占位——
    万一以后哪个版本要读它，拿到的是合格式的数据。
    """
    vtt = find_vtt(video, lang)
    if vtt is None:
        return None
    groups, kind = parse_srt(vtt)
    if groups is None or len(groups) < 3:
        return None
    label = f"{kind} · {vtt.name} · {len(groups)} 句"
    return to_srt(groups), stable_json(groups, source=vtt.name), label


def stable_json(cues: list[tuple[float, float, str]], *, source: str = "",
                mark: str = "youtube-captions",
                note: str = "由星译从 YouTube 字幕直接生成，未做语音识别") -> str:
    """按引擎「稳定化」产物的结构，从字幕反过来写一份

    真的结构（实测）：
      {"segments": [{start, end, words, text, confidence, no_speech_prob, lang, …}],
       "vad_timeline": {"intervals": [{start, end}, …]},
       "metadata": {...}}
    我们没有词级对齐，words 留 None；vad_timeline 用句子区间顶上。
    """
    import json

    segs = [
        {
            "start": round(s, 3),
            "end": round(e, 3),
            "words": None,
            "text": text.replace("\n", " "),
            "confidence": None,
            "no_speech_prob": None,
            "lang": None,
            "vad_weighted_energy_db": None,
        }
        for s, e, text in cues
    ]
    payload = {
        # metadata 放最前面：清洗时要靠这里的标记认出「这是我们放的」，
        # 而后面的 segments 动辄几十万字符，标记排后面就得整份读进来才能看见
        "metadata": {
            "source": mark,
            "source_file": source,
            "backend": mark,
            "note": note,
        },
        "segments": segs,
        "vad_timeline": {"intervals": [{"start": s["start"], "end": s["end"]} for s in segs]},
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_srt(vtt: Path) -> tuple[list[tuple[float, float, str]] | None, str]:
    """vtt → 整句列表。自动轨走词流重建，人工轨走 cue 合并"""
    blocks = read_blocks(vtt)
    if not blocks:
        return None, ""
    words = auto_words(blocks)
    # 自动轨的比例远高于人工轨（词级标记铺满整个文件）
    tagged = sum(1 for _s, _e, body in blocks if any("<c>" in ln for ln in body))
    if tagged >= max(4, len(blocks) // 3):
        groups = group_words(words)
        return groups, "YouTube 自动字幕"
    cues = parse_vtt(vtt)
    cues = [c for c in cues if _RE_CUE_MARK.sub("", c[2]).strip()]
    rolling = sum(1 for i in range(1, len(cues)) if cues[i][2].startswith(cues[i - 1][2]))
    if rolling >= max(2, len(cues) // 4):
        cues = dedupe_rolling(cues)
    return group_sentences(split_cues(cues)), "YouTube 字幕"


if __name__ == "__main__":       # 手工验证：python ytsub.py <视频或 vtt>
    import sys

    p = Path(sys.argv[1])
    if p.suffix == ".vtt":
        groups, kind = parse_srt(p)
        print(f"{kind}：原始 {len(read_blocks(p))} 块 → {len(groups or [])} 句\n")
        print(to_srt(groups or [])[:1500])
    else:
        got = build_srt(p, sys.argv[2] if len(sys.argv) > 2 else "")
        print(got[1] if got else "（没找到字幕）")
        if got:
            print(got[0][:1500])
