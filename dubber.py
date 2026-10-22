#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配音模块：SRT -> 中文配音成片（MiMo 逐句合成 + ffmpeg adelay 时间轴装配）

对外接口（可 import，供 GUI 直接调用，进度/取消都走 Job）：
    from dubber import dub
    stats = dub(srt, video, out, api_key=key, voice="冰糖", job=job, progress_cb=cb)

基于 aivt/tools/make_dub.py 移植（自有代码），增加了：
  - progress_cb(phase, done, total) 进度回调
  - job（取消标志 + 子进程受管）
  - 缓存按 hash(文本+音色) 命名（修掉"换文本/换音色静默复用旧音频"的坑）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import urllib.request

from job import Job
import diag
import mirrors
import tools

MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-tts"
#: 音色克隆得换模型名：普通那档的 `voice` 只认内置音色名，塞参考音 data URI 会直接
#: 报 `Param Incorrect / Unknown voice: data:audio/...`（2026-10-13 实测，一度被 429 掩盖）
VOICECLONE_MODEL = "mimo-v2.5-tts-voiceclone"


# ---------- 基础工具 ----------

def parse_srt(p: Path | str):
    """返回 [(start, end, text), ...]"""
    srt = Path(p).read_text(encoding="utf-8-sig")
    pat = re.compile(
        r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)", re.S)

    def sec(h, mi, s, ms):
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000

    out = []
    for m in pat.finditer(srt):
        text = " ".join(m.group(10).split())
        if text:
            out.append((sec(m.group(2), m.group(3), m.group(4), m.group(5)),
                        sec(m.group(6), m.group(7), m.group(8), m.group(9)), text))
    return out


#: MiMo 的 `user` 消息是**风格指令**（官方文档的例子就是 "Fast pace, rising pitch at
#: the end."），不是占位符——我们以前一直传空字符串，等于白白扔掉一个控制口。
#: 实测（2026-10-13，同一句话 49 字、音色冰糖）：
#:     空 8.96s（5.5 字/秒）/ 语速稍快 8.16s / 语速偏快 7.68s（6.4 字/秒）
#: 光靠提示词就省 14%，等于替掉 1.17× 的硬加速，而且是模型自己说快，韵律自然。
#: ⚠ 措辞会互相打架：写过「语速偏快…像讲课时**娓娓道来**」——「娓娓道来」是不紧不慢
#: 的意思，实测反而只有 4.75 字/秒。
#: 2026-10-17 用户要求默认用标准语速，所以默认值改成下面这句自然语速的版本；
#: 「语速偏快」那版只在个别句子塞不进时间窗时由 FASTER_STYLE 使用。
DEFAULT_STYLE = "语速自然、平稳，像正常讲解一样，清晰易懂。"
#: 英文配音的默认风格（中文那句指令对英文 TTS 未必合适，单独给一句）
DEFAULT_STYLE_EN = "Speak natural, fluent English at a moderate pace, clear and expressive."
#: 个别句子还是塞不进时间窗时，再请它快一档（只对这几句用，别整片换，免得听着忽快忽慢）
FASTER_STYLE = "语速再快一些，但保持清晰自然，不要赶、不要吞字。"


def synth_mimo(text: str, out_path: Path, key: str, voice: str, retries: int = 4,
               timeout: float = 60.0, style: str = "",
               model: str = MIMO_MODEL, params: dict | None = None) -> str | None:
    """单句 MiMo 合成，成功返回 None，失败返回错误信息（指数退避重试）

    timeout 取 60s：正常单句 3~20s；请求挂起（服务端不回也不断）时快速失败交给重试，
    否则个别句会卡住整条流水线（实测 urlopen 的长超时对"慢速挂起"不生效）。
    style 是「怎么读」的自然语言指令，见 DEFAULT_STYLE。
    model 走音色克隆时要传 VOICECLONE_MODEL（普通那档不认参考音）。
    """
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": style or ""},
                     {"role": "assistant", "content": text}],
        "audio": {"format": "wav", "voice": voice},
    }, ensure_ascii=False).encode("utf-8")
    opener = mirrors.opener(params or {})
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(MIMO_URL, data=payload, headers={
                "Content-Type": "application/json", "api-key": key})
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            b64 = data["choices"][0]["message"]["audio"]["data"]
            out_path.write_bytes(base64.b64decode(b64))
            if out_path.stat().st_size > 500:
                return None
            last = "返回空音频"
        except urllib.error.HTTPError as e:
            # 给用户看的只有人话（这句会一路冒到「第 N 句合成失败」那个提示里）
            last = {401: "密钥无效", 403: "密钥没权限"}.get(e.code, f"服务返回 {e.code}")
            if e.code == 429:
                # 限流不是失败：等久一点再试。开了音色克隆时每个请求都驮着参考音
                # （约 130KB），比纯文本请求更容易撞限流（2026-10-13 实测 6 并发秒撞）
                time.sleep(min(45, 8 * (attempt + 1)))
                continue                     # 这个分支自己睡过了，别再叠加下面的退避
            if e.code in (401, 403):
                return last                  # key 不对，重试多少次都一样
        except Exception as e:  # noqa: BLE001
            last = "连不上，检查网络或代理设置"
            # 系统代理指着已经关掉的本地端口：清缓存后重新判定，下一次多半会走直连
            if "10061" in str(e):
                mirrors.forget_proxy_cache()
                opener = mirrors.opener(params or {})
        time.sleep(min(8, 2 ** attempt))
    return last


def probe_dur(path: Path) -> float:
    from job import no_window_kwargs
    r = subprocess.run([tools.path("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True, **no_window_kwargs())
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _cache_name(text: str, voice: str, suffix: str = "", style: str = "") -> str:
    """缓存名 = hash(文本+音色+风格) 前 12 位

    风格必须进哈希：同一句话配不同风格是不同的音频，漏了它就会把旧的念法缓存回来。
    """
    h = hashlib.sha1(f"{voice}\x00{style}\x00{text}".encode("utf-8")).hexdigest()[:12]
    return f"{h}{suffix}.wav"


#: TTS 会把 CLI / CD 这类大写字母当成罗马数字念（实测 CLI=151、CD=400）。
#: 只匹配全大写、且只由罗马数字字母组成的独立 token；单字母（I）不碰。
_RN_TOKEN = re.compile(r"(?<![A-Za-z0-9])([IVXLCDM]{2,})(?![A-Za-z0-9])")


def _tts_text(text: str) -> str:
    """把送给 TTS 的罗马数字形缩写拆成单字母，字幕原文保持不变"""
    return _RN_TOKEN.sub(lambda m: " ".join(m.group(1)), text)


#: 缓存目录的总体积上限；超了从最旧的开始删（删掉只是下次重新合成，不会出错）
_CACHE_MAX_BYTES = 1_500_000_000


def default_cache_dir() -> Path:
    """配音缓存的**持久**目录 —— 跨运行、跨任务共用。

    ⚠️ 以前调用方传的是「本次任务输出目录下的 tts_cache」，而任务目录名带时间戳
       （pipeline：`<视频名>-<stamp>`）—— 每次跑都是一个全新的空目录，**缓存从来
       没命中过**：同一个视频重跑一遍就是全量重新合成（白花钱、白等一遍）。
       缓存键已含 文本+音色+风格（见 `_cache_name`；克隆模式下音色里还含参考音哈希），
       所以放全局共用是安全的：不同视频里相同的一句 = 同一份音频。

    子目录按合成模型名打散：哪天换了模型，新目录开工，旧缓存不会拿旧念法顶上。
    """
    tag = hashlib.sha1(f"{MIMO_MODEL}\x00{VOICECLONE_MODEL}".encode("utf-8")).hexdigest()[:8]
    import paths
    return paths.resolve("dub_cache") / tag


def _prune_cache(root: Path) -> None:
    """把缓存体积压回上限以内（按 mtime 从旧到新删）。

    纯加速用的东西 —— 出任何问题一律当没这回事，绝不能因为它把配音搞挂。
    """
    try:
        files = [p for p in root.rglob("*.wav") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= _CACHE_MAX_BYTES:
            return
        for p in sorted(files, key=lambda f: f.stat().st_mtime):
            if total <= _CACHE_MAX_BYTES:
                break
            try:
                total -= p.stat().st_size
                p.unlink()
            except OSError:
                continue
    except OSError:
        pass


# ---------- 断句合并 + 静音裁剪 ----------
#
# 症状：配音听着是一句一句往外蹦，尤其在字幕切开的地方。
# 两个原因，都实测过（2026-10-15）：
#   1. 字幕的换行处**不是句子边界**。成品字幕是按显示时长切的，「编程智能体」这种词
#      会被从中间劈开（实测 36 行里有 13 处）。一行一次合成 → 模型当成一整句念，
#      句末降调收尾、下一行再从零起调。
#   2. 每段音频自带约 0.1 秒头静音 + 0.25 秒尾静音（实测），而字幕行之间基本没空隙
#      → 每句凭空多出 0.35 秒死寂，正好把第 1 条的断点放大。

#: 源语言字幕里「这句话说完了」的标点。中文成品字幕**行尾一律没有标点**（ASR 产物），
#: 所以「断没断句」这个信息只能从源语言那份拿。
_SENT_END = ".!?。！？…"
#: 一组最多并几行 —— 只是兜底，真正决定「要不要并」的是下面的时长上限
_MAX_GROUP = 12
#: 一组最长读多少秒（估算）。超过就分段，别让一次合成读半分钟以上。
#: ⚠️ 以前这里写的是「最多并 3 行」，长句会被硬切成两段、各念一次，
#:    听感就是「上一句最后几个字停很久、接不上下一句」（2026-10-21 用户报）。
#:    按**时长**限比按行数限合理：一行字幕多长并不固定。
_MAX_GROUP_SEC = 20.0
#: 估算时长给的宽限（秒）；超出那点交给后面的 fit/atempo 兜
_GROUP_SLACK = 0.4
#: 放慢语速填空白时，句末至少留这么久的自然停顿
_PAUSE_KEEP = 0.30


def _is_cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿"


def _est_sec(text: str) -> float:
    """粗估这段文本要读多久（只用来给合并分组设上限，别拿它当精确值）

    中日韩字算 1 个单位，拉丁字母/数字算 0.4（"Claude Code" 比 11 个汉字快得多），
    空格标点算 0.3；除以 5.5 单位/秒 —— 这是标准语速（空风格）实测出来的速度。
    """
    units = 0.0
    for ch in text:
        units += 1.0 if _is_cjk(ch) else (0.4 if ch.isalnum() else 0.3)
    return units / 5.5


def _cont_flags(segs: list, src_srt: Path | str | None) -> list[bool]:
    """cont[k] = 「第 k+1 行是第 k 行的续句」

    判据：源语言字幕里**结束时间与第 k 行相同**的那一条，如果不以 . ! ? 结尾，
    说明这句话还没说完，下一行是接着说的。

    拿不到源语言字幕、或时间戳对不上，一律返回 False = 不合并 ——
    **宁可不合，也别合错**（合错会把两个完整的句子读成一串）。

    ⚠️ 要传 `-raw.srt`，**别用 `-corrected.srt`**：实测 raw 的结束时间与中文成品
    逐行精确到 0.000 秒（36 行全中），corrected 差到 0.6 秒，映射不可靠。
    """
    flags = [False] * len(segs)
    if not src_srt:
        return flags
    p = Path(src_srt)
    if not p.exists():
        return flags
    try:
        src = parse_srt(p)
    except Exception:  # noqa: BLE001
        return flags
    if not src:
        return flags
    for k in range(len(segs) - 1):
        target = segs[k][1]
        best, j = 1e9, -1
        for i, (_a, z, _t) in enumerate(src):
            d = abs(z - target)
            if d < best:
                best, j = d, i
        if j < 0 or best > 0.20:
            continue                       # 对不上就不合
        tail = src[j][2].rstrip()
        if tail and tail[-1] not in _SENT_END:
            flags[k] = True
    return flags


def _group_segs(segs: list, cont: list[bool]) -> list[list[int]]:
    """把「续句」的相邻行并成一组：`[[行号...], ...]`

    组内只合成**一次**，音频放在该组第一行的起点上。
    """
    groups: list[list[int]] = []
    n = len(segs)
    i = 0
    while i < n:
        g = [i]
        j = i + 1
        while j < n and cont[j - 1] and len(g) < _MAX_GROUP:
            # 并进来之后，到下一组开口之前还读得完吗
            end = segs[j + 1][0] if j + 1 < n else segs[j][1]
            joined = " ".join(segs[t][2] for t in g) + " " + segs[j][2]
            est = _est_sec(joined)
            if est > (end - segs[i][0]) + _GROUP_SLACK:
                break
            if est > _MAX_GROUP_SEC:       # 一次合成别读太长，按估算时长切
                break
            g.append(j)
            j += 1
        groups.append(g)
        i = g[-1] + 1
    return groups


#: 裁首尾静音：开头裁到只剩 50ms，结尾裁到只剩 60ms。
#: `silenceremove` 只能裁开头，所以尾巴靠 areverse 翻过来当开头裁、再翻回去。
#: 实测（8/8 成功）：头静音 0.07~0.18s → 0.00~0.05s，尾静音 0.14~0.42s → 0.00~0.05s。
_TRIM_FILTER = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,"
                "areverse,"
                "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.06,"
                "areverse")


def _trimmed(p: Path, job: Job) -> Path:
    """裁掉一段音频的首尾静音，结果缓存在旁边（`xxx_trim.wav`）

    裁失败就返回原件 —— 不能因为裁静音把整句配音搞没了。
    """
    tp = p.with_name(p.stem + "_trim.wav")
    if tp.exists() and tp.stat().st_size > 500:
        return tp
    r = job.run([tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(p),
                 "-filter:a", _TRIM_FILTER, str(tp)],
                capture_output=True, text=True)
    if r.returncode == 0 and tp.exists() and tp.stat().st_size > 500:
        return tp
    return p


# ---------- 主流程 ----------

# 单次 ffmpeg 的句数上限：命令行里每句要占「相对文件名 + adelay 滤镜」约 50 字符，
# Windows 命令行上限 32767 → 大约 600 句封顶（约 40 分钟视频）。超了就分批装配。
_ONE_SHOT_MAX = 400
_BATCH = 200


def _rel(work: Path, p: Path) -> str:
    """工作目录里的文件用相对名（cwd=work），一句只占十几个字符"""
    try:
        return str(p.relative_to(work))
    except ValueError:
        return str(p)


def _run(cmd: list[str], work: Path, job: Job) -> None:
    r = job.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(work))
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 装配失败: {(r.stderr or '')[-800:]}")


def _delays(names: list[str], starts: list[float], first_index: int) -> tuple[str, str]:
    """逐句 adelay 对位。返回 (滤镜串, 各路标签串)"""
    graph = "".join(
        f"[{first_index + k}:a]adelay={int(round(s * 1000))}|{int(round(s * 1000))}[d{k}];"
        for k, s in enumerate(starts))
    return graph, "".join(f"[d{k}]" for k in range(len(names)))


def _has_audio(video: Path) -> bool:
    """视频有没有音轨（没有的话就不能去混 [0:a]，否则整条滤镜直接报错）"""
    from job import no_window_kwargs
    r = subprocess.run([tools.path("ffprobe"), "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, **no_window_kwargs())
    return bool((r.stdout or "").strip())


def _assemble_one(video: Path, names: list[str], starts: list[float], dest: Path, *,
                  work: Path, orig_vol: float, voice_vol: float, job: Job) -> None:
    """一次成型：原视频 + 全部句子"""
    inputs = ["-i", str(video)]
    for n in names:
        inputs += ["-i", n]
    graph, labels = _delays(names, starts, 1)
    graph += f"{labels}amix=inputs={len(names)}:duration=longest:normalize=0,volume={voice_vol}dB"
    if _has_audio(video):
        # duration=longest：[orig] 是整条视频，配音轨短于视频时成片不能被截断
        # （以前写的是 duration=first，最后一句字幕之后的画面会被 -shortest 切掉）
        graph += "[dub];" + f"[0:a]volume={orig_vol}[orig];" \
                 "[dub][orig]amix=inputs=2:duration=longest:normalize=0[outa]"
    else:
        # 没音轨的视频：配音后补静音撑满画面长度
        # （用 apad=whole_dur 而不是 apad + -t：这个 ffmpeg 构建下后者会挂住不退出）
        graph += f",apad=whole_dur={probe_dur(video):.3f}[outa]"
    _run([tools.path("ffmpeg"), "-v", "error", "-y"] + inputs + [
        "-filter_complex", graph, "-map", "0:v", "-map", "[outa]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)], work, job)


def _assemble_part(names: list[str], starts: list[float], total: float, dest: Path, *,
                   work: Path, voice_vol: float, job: Job) -> None:
    """分批：混出这一批的声轨，再补静音撑到与时间轴等长

    这里不加"静音底"那一路输入 —— 多一路输入会让 amix 的声道/增益跟一次成型不一致
    （实测差了 3dB）。用 apad + -t 补长度，两条路径的混音参数就完全一样了。
    """
    inputs: list[str] = []
    for n in names:
        inputs += ["-i", n]
    graph, labels = _delays(names, starts, 0)
    graph += (f"{labels}amix=inputs={len(names)}:duration=longest:normalize=0,"
              f"volume={voice_vol}dB,apad=whole_dur={total:.3f}[out]")
    _run([tools.path("ffmpeg"), "-v", "error", "-y"] + inputs + [
        "-filter_complex", graph, "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k", str(dest)], work, job)


def _assemble_final(video: Path, parts: list[Path], dest: Path, *, work: Path,
                    orig_vol: float, job: Job) -> None:
    """分批的收尾：各分轨在时间上互不重叠，直接相加，再与原声混"""
    inputs = ["-i", str(video)]
    for p in parts:
        inputs += ["-i", _rel(work, p)]
    labels = "".join(f"[{i + 1}:a]" for i in range(len(parts)))
    if _has_audio(video):
        graph = (f"[0:a]volume={orig_vol}[orig];"
                 f"{labels}amix=inputs={len(parts)}:duration=first:normalize=0[dub];"
                 f"[dub][orig]amix=inputs=2:duration=longest:normalize=0[outa]")
    else:
        graph = (f"{labels}amix=inputs={len(parts)}:duration=first:normalize=0,"
                 f"apad=whole_dur={probe_dur(video):.3f}[outa]")
    _run([tools.path("ffmpeg"), "-v", "error", "-y"] + inputs + [
        "-filter_complex", graph, "-map", "0:v", "-map", "[outa]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)], work, job)


def _assemble(video: Path, use_paths: list[Path], segs: list, out_path: Path, *,
              work: Path, orig_vol: float, voice_vol: float, job: Job) -> None:
    """把逐句配音拼到时间轴上，再和原声混音出片

    命令行长度是要命的：Windows 上限约 32767 字符，而每句都要一个 `-i` 和一个 adelay
    滤镜（这个 ffmpeg 构建没有 -filter_complex_script，滤镜没法挪进文件）。40 分钟以上的
    视频会直接把上限撑爆，报错只有一句"命令行太长"，极难查。
    所以：① wav 用工作目录里的相对名；② 句数太多时分批混成整条时间轴的分轨再合并。
    """
    video = Path(video)
    names = [_rel(work, p) for p in use_paths]
    starts = [s for s, _e, _t in segs]
    if len(segs) <= _ONE_SHOT_MAX:
        _assemble_one(video, names, starts, Path(out_path), work=work, job=job,
                      orig_vol=orig_vol, voice_vol=voice_vol)
        return

    total = probe_dur(video) or (max(e for _s, e, _t in segs) + 2.0)
    parts: list[Path] = []
    for k, start in enumerate(range(0, len(segs), _BATCH)):
        idx = list(range(start, min(start + _BATCH, len(segs))))
        part = work / f"dub_part{k:03d}.m4a"
        _assemble_part([names[i] for i in idx], [starts[i] for i in idx], total, part,
                       work=work, job=job, voice_vol=voice_vol)
        parts.append(part)
        job.check()
    _assemble_final(video, parts, Path(out_path), work=work, job=job, orig_vol=orig_vol)
    for p in parts:
        try:
            p.unlink()
        except OSError:
            pass


def _voice_data_uri(ref: Path) -> tuple[str, str]:
    """人声克隆的参考音 → (data URI, 缓存用的短标识)

    先转 mp3 再传：12 秒参考音 wav 是 380KB、base64 后约 500KB，而**每句请求都要带一遍**
    （500 句 = 250MB 上传）；转成 64k mp3 后只剩约四分之一。
    短标识进缓存名 —— 参考音换了就是另一个音色，不能命中旧音频。
    """
    from job import no_window_kwargs
    mp3 = ref.with_name(ref.stem + ".ref.mp3")
    if not (mp3.exists() and mp3.stat().st_size > 1000):
        subprocess.run([tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(ref),
                        "-ac", "1", "-ar", "32000", "-b:a", "64k", str(mp3)],
                       capture_output=True, text=True, **no_window_kwargs())
    ok = mp3.exists() and mp3.stat().st_size > 1000
    raw = (mp3 if ok else ref).read_bytes()
    mime = "audio/mpeg" if ok else "audio/wav"
    return (f"data:{mime};base64," + base64.b64encode(raw).decode("utf-8"),
            f"clone{hashlib.sha1(raw).hexdigest()[:8]}")


def dub(srt_path: Path | str, video: Path | str, out_path: Path | str, *,
        api_key: str, voice: str = "冰糖", jobs: int = 6, fit: bool = True,
        fit_max: float = 1.35, fit_slow: float = 0.88, speed_bias: float = 1.0,
        orig_vol: float = 0.15, voice_vol: float = 6.0,
        cache_dir: Path | str | None = None, job: Job | None = None,
        progress_cb=None, style: str = DEFAULT_STYLE,
        clone_ref: Path | str | None = None, clone_local: bool = False,
        params: dict | None = None, src_srt: Path | str | None = None,
        merge_sentences: bool = True, trim_silence: bool = True) -> dict:
    """SRT + 视频 -> 配音成片。返回统计 dict。progress_cb(done, total) 汇报合成进度。

    style          「怎么读」的风格指令（见 DEFAULT_STYLE），空字符串 = 不指定
    speed_bias     整体语速倍率（1.0 = 不额外加速；1.05 = +5%；1.10 = +10%）。
                   在「裁静音之后、时长适配之前」统一加速，后面的自动适配再按结果算。
    clone_ref      人声克隆的参考音；给了就用它复刻的音色读，忽略 voice
    clone_local    True = 本机那套（IndexTTS-2.5，免费不联网，要先下载），
                   False = MiMo 云端克隆（默认）
    src_srt        **源语言**字幕（`<任务名>-raw.srt`）。给上它才能判断「字幕这一行的
                   换行处是不是句子边界」，从而把被切开的半句话并成一次合成。
                   不给就退回「一行一句」，只是听着会比较断。见 `_cont_flags`。
    merge_sentences / trim_silence  两个开关都是给排查用的，正常别关
    """
    job = job or Job()
    segs = parse_srt(srt_path)
    if not segs:
        raise ValueError("字幕为空或解析失败")

    # 把「被字幕切开、其实是同一句」的相邻行并成一组，一组只合成一次。
    # 见本文件「断句合并 + 静音裁剪」那一段的说明。
    groups = (_group_segs(segs, _cont_flags(segs, src_srt)) if merge_sentences
              else [[i] for i in range(len(segs))])
    #: 合成单元 —— 下面一律按「组」走，不再是「字幕行」
    units = [{"text": _tts_text(" ".join(segs[j][2] for j in g)),
              "start": segs[g[0]][0], "end": segs[g[-1]][1]} for g in groups]
    n_merged = len(segs) - len(units)
    if n_merged:
        diag.write(f"[dub] 把 {n_merged} 处被字幕切开的半句话并回整句"
                   f"（{len(segs)} 行 → {len(units)} 组）", "run")

    work = Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp(prefix="dub_"))
    work.mkdir(parents=True, exist_ok=True)
    if cache_dir:
        # 持久缓存按模型分了好几个子目录，清理时连兄弟目录一起看
        _prune_cache(work.parent if work.parent.name == "dub_cache" else work)

    style = (style or "").strip()
    voice_arg, voice_key, model = voice, voice, MIMO_MODEL
    if clone_ref:
        voice_arg, voice_key = _voice_data_uri(Path(clone_ref))
        model = VOICECLONE_MODEL          # ← 克隆必须换模型名，否则报 Unknown voice
        # 克隆模型的限流比普通音色紧得多：2026-10-13 实测 6 路并发约一半 429
        # （响应体 `"type": "limitation"`）；每个请求还要驮着 130KB 参考音。
        # 所以克隆一律压低并发，宁可慢也别把整条流水线卡在重试上。
        jobs = min(jobs, 2)

    # 1) 逐组合成（并发）
    # ⚠️ 2026-10-22 试过「第一遍就按估算时长预加速」，**实测不成立**：
    #    _est_sec 是 5.5 单位/秒的粗估，对这批判定超长的句子实际慢了 40%，
    #    8 个样本按预测语速合成后仍然全部超窗（有的还是 2 倍）。已改回 1.0，
    #    超长句继续走下面「整批重念一档」那条路（实测 113 组里只有 19 组
    #    连重念+最大变速都塞不下，为省那 2 分钟去牺牲韵律不值得）。
    paths = [work / _cache_name(u["text"], voice_key, style=style) for u in units]
    todo = [i for i in range(len(units))
            if not (paths[i].exists() and paths[i].stat().st_size > 500)]
    if progress_cb:
        progress_cb(0, len(units))

    ex = ThreadPoolExecutor(max_workers=jobs)
    # 克隆那条路更容易撞限流，多给两次重试（每次退避最长 45 秒）
    tries = 6 if clone_ref else 4
    try:
        if clone_local:
            # 本机复刻（IndexTTS-2.5）：模型加载一次要十几秒，所以**整批一次跑完** ——
            # 一句起一个进程会慢到没法用。参考音就是原说话人的那一小段。
            if todo:
                import localclone
                items = [(units[i]["text"], paths[i], 1.0) for i in todo]
                base = len(units) - len(todo)
                res = localclone.synth(
                    items, clone_ref, params or {}, job, work,
                    progress_cb=(lambda d, _t: progress_cb(base + d, len(units)))
                    if progress_cb else None)
                if res["fail"]:
                    first = res["errors"][0] if res["errors"] else ""
                    raise RuntimeError(f"本机语音克隆合成失败 {res['fail']} 句：{first}")
        else:
            futs = {ex.submit(synth_mimo, units[i]["text"], paths[i], api_key, voice_arg,
                              retries=tries, style=style, model=model): i for i in todo}
            done = len(units) - len(todo)
            for fut in as_completed(futs):
                job.check()
                i = futs[fut]
                err = fut.result()
                if err:
                    raise RuntimeError(f"第 {i + 1} 段 MiMo 合成失败: {err}")
                done += 1
                if progress_cb:
                    progress_cb(done, len(units))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    job.check()

    # 2) 裁首尾静音 → 时长体检 → 语速适配（防叠音）
    #    ⚠️ 静音必须**先裁再量长**：MiMo 每条自带约 0.35 秒首尾静音，不裁的话
    #    「塞不下」是假的，会白白多调一次 API 去请它说快一档，还白做一次变速。
    use_paths = [_trimmed(p, job) for p in paths] if trim_silence else list(paths)
    with ThreadPoolExecutor(max_workers=8) as pool:
        durs = list(pool.map(probe_dur, use_paths))

    # 2.1) 用户选的「整体语速」：先给每一句统一加速，再走后面的时长适配。
    #      放在这里的两个原因：① 静音已经裁掉，量出来的时长是准的；
    #      ② 先加速再算「塞不塞得下」，后面就不会把刚加速的句子又判成「太短」放回去
    #      （所以下面「放慢填空档」那一段在 speed_bias > 1 时整个跳过）。
    if speed_bias > 1.001:
        pct = int(round(speed_bias * 100))
        biased: list[Path] = []
        for i, p in enumerate(use_paths):
            fp = p.with_name(_cache_name(units[i]["text"], voice_key, f"_sp{pct}", style=style))
            if not (fp.exists() and fp.stat().st_size > 500):
                r = job.run([tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(p),
                             "-filter:a", f"atempo={speed_bias:.4f}", str(fp)],
                            capture_output=True, text=True)
                if r.returncode != 0 or not (fp.exists() and fp.stat().st_size > 500):
                    fp = p          # 变速失败就退回原音，别让整条配音挂掉
            biased.append(fp)
        use_paths = biased
        with ThreadPoolExecutor(max_workers=8) as pool:
            durs = list(pool.map(probe_dur, use_paths))

    def _room(idx: int) -> float:
        """这一组最多能占多久 —— 到下一组开口为止

        ⚠ 以前只看自己的字幕槽位（超 0.35 秒就加速）。可槽位之间本来就有空隙：
        后面明明空着半秒，还硬压一遍，纯粹是自己给自己加戏。
        """
        own = units[idx]["end"] - units[idx]["start"]
        if idx + 1 < len(units):
            return max(own, units[idx + 1]["start"] - units[idx]["start"])
        return own

    over = [(i, durs[i], _room(i)) for i in range(len(units))
            if durs[i] > _room(i) + 0.12]
    fitted = resynth = 0
    if over:
        # 诊断：超长多少、超得有多狠。决定了「重念」和「直接变速」该怎么分。
        # 1.00-1.05 这种轻微超长其实 atempo 一下就听不出来，不值得再合成一遍。
        ratios = sorted(d / max(0.1, r) for _i, d, r in over)
        def _cnt(lo: float, hi: float) -> int:
            return sum(1 for x in ratios if lo <= x < hi)
        diag.write(
            "[dub] 超长 %d/%d 组：1.00-1.05 %d，1.05-1.10 %d，1.10-1.20 %d，>1.20 %d"
            % (len(over), len(units), _cnt(1.0, 1.05), _cnt(1.05, 1.10),
               _cnt(1.10, 1.20), _cnt(1.20, 99)), "run")

    if fit and over and clone_local:
        # 本机引擎**加载一次要二三十秒**，所以「说快一档」也得整批一次做完 ——
        # 一句起一个进程的话，光加载就够呛（2026-10-14 想清楚这件事）。
        import localclone
        need = []
        for i, _dur, _r in over:
            fp2 = paths[i].with_name(
                _cache_name(units[i]["text"], voice_key, "_fast", style=FASTER_STYLE))
            if not (fp2.exists() and fp2.stat().st_size > 500):
                need.append((units[i]["text"], fp2, 0.85))     # 0.85 = 让它说快一档
        if need:
            r = localclone.synth(need, clone_ref, params or {}, job, work)
            resynth += r["ok"]
            if r["fail"]:
                diag.write(f"[dub] 本机引擎「说快一档」失败 {r['fail']} 句，那些句子走变速", "run")

    if fit and over:
        for i, dur, room in over:
            job.check()
            # 先请模型自己快一档：韵律是自然的，代价是一次额外合成（结果照样进缓存）。
            # 本机那批上面已经一次做完了，这里只剩云端（MiMo）这一条路要逐句补。
            fp2 = paths[i].with_name(
                _cache_name(units[i]["text"], voice_key, "_fast", style=FASTER_STYLE))
            if fp2.exists() and fp2.stat().st_size > 500:
                pass                          # 上次已经合成过，直接用（别重复调 API）
            elif not clone_local and synth_mimo(units[i]["text"], fp2, api_key, voice_arg,
                                                style=FASTER_STYLE, model=model,
                                                params=params) is None:
                resynth += 1
            if fp2.exists() and fp2.stat().st_size > 500:
                fp2t = _trimmed(fp2, job) if trim_silence else fp2
                fast = probe_dur(fp2t)
                if fast and fast < dur:
                    use_paths[i], dur = fp2t, fast
                    durs[i] = fast
            if dur <= room:
                continue
            # 还塞不下才动 ffmpeg 硬加速（最后手段）
            tempo = min(fit_max, dur / max(0.3, room - 0.1))
            if tempo <= 1.02:
                continue
            # ⚠️ 后缀要带上「裁过静音」的标记：不裁时算出来的 _fit 和裁过的是**不同的音频**，
            #    共用缓存名会静默复用旧文件（这正是以前那套缓存坑的翻版）。
            suffix = ("_fit2" if "_fast" in use_paths[i].stem else "_fit") + \
                     ("t" if trim_silence else "")
            fp = paths[i].with_name(_cache_name(units[i]["text"], voice_key, suffix, style=style))
            r = job.run([tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(use_paths[i]),
                         "-filter:a", f"atempo={tempo:.4f}", str(fp)],
                        capture_output=True, text=True)
            if r.returncode == 0 and fp.exists() and fp.stat().st_size > 500:
                use_paths[i] = fp
                fitted += 1

    # 2.5) 反向适配：读得比窗口**短太多**时，放慢一点把空档填上
    #      中文比英文短得多（实测同一句：中文 4.4 秒 vs 英文 8.2 秒的窗口），所以
    #      每条读完都会剩一大段空白 —— 改前平均每行留 0.85 秒、最长 2.6 秒死寂，
    #      听着就是「念一句、静一会儿、再念一句」。放到 fit_slow 为止就不放了：
    #      再慢就是刻意拖长，反而假。
    slowed = 0
    over_idx = {i for i, _d, _r in over}
    # 用户主动选了「整体加速」时不再做「放慢填空档」—— 那会把刚加速的效果又拉回去
    if fit and speed_bias <= 1.001:
        for i in range(len(units)):
            if i in over_idx:
                continue                   # 这些是「太长」，上面已经处理过
            job.check()
            dur, room = durs[i], _room(i)
            target = room - _PAUSE_KEEP    # 给句末留一点自然停顿
            if dur <= 0.3 or target <= 0.5 or dur >= target:
                continue
            tempo = max(fit_slow, dur / target)
            if tempo > 0.97:               # 只慢不到 3% 就不折腾了
                # ⚠️ 以前这里是 0.93（不到 7% 就跳过）。对长句来说 7% 就是
                #    一两秒的死寂（实测 20 秒的组留了 1.5 秒），听感正是
                #    「上一句最后几个字停很久」（2026-10-21 用户报）。
                continue
            suffix = "_slow" + ("t" if trim_silence else "")
            fp = paths[i].with_name(_cache_name(units[i]["text"], voice_key, suffix, style=style))
            if not (fp.exists() and fp.stat().st_size > 500):
                r = job.run([tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(use_paths[i]),
                             "-filter:a", f"atempo={tempo:.4f}", str(fp)],
                            capture_output=True, text=True)
                if r.returncode != 0 or not (fp.exists() and fp.stat().st_size > 500):
                    continue
            use_paths[i] = fp
            durs[i] = probe_dur(fp) or dur
            slowed += 1

    # 3) 装配时间轴（adelay 逐组对位 + 原声低音量混音）
    job.check()
    # _assemble 只用到 (起点, 终点, 文本) —— 把「组」这条时间轴传进去，函数本身不用改
    timeline = [(u["start"], u["end"], u["text"]) for u in units]
    _assemble(Path(video), use_paths, timeline, Path(out_path),
              work=work, orig_vol=orig_vol, voice_vol=voice_vol, job=job)
    job.check()

    return {"segments": len(segs), "units": len(units), "merged": n_merged,
            "over_slot": len(over), "fitted": fitted, "slowed": slowed,
            "speed_bias": round(speed_bias, 3),
            "resynth": resynth, "work_dir": str(work)}
