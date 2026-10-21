#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SRT -> ASS 字幕转换（带描边样式，供 ffmpeg 烧录）

用法:
  CLI:  python srt2ass.py <in.srt> <out.ass> [--font ...] [--size 42] [--margin-v 30]
                           [--spacing 0] [--color "#FFFFFF"]
  模块: from srt2ass import srt_to_ass;  srt_to_ass(srt, ass)
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

_PAT = re.compile(
    r"\d+\s*\n(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)", re.S)


def _fit_size(text: str, base: float, play_w: int, margin: int = 20,
              spacing: float = 0.0) -> float:
    """按一行能放下来估字号：中文按 1.0 字宽、英文按 0.56 字宽。

    这只是估算（不同字体略有差异），目的是尽量让一条字幕保持一行；
    估出来比 base 小就用小的，估出来更大就封顶在 base。
    """
    units = 0.0
    for ch in text:
        units += 1.0 if ord(ch) > 0x2E7F else 0.56
    if units <= 0:
        return base
    # 字间距是像素值，正数会让整行更宽、负数更紧
    extra = spacing * max(0, len(text) - 1)
    fit = (play_w - margin * 2 - extra) / units
    return max(24.0, min(base, fit))      # 低于 24 就太小了，宁可稍微溢出


def _rgb(color: str) -> tuple[int, int, int]:
    """#RRGGBB -> (r, g, b)；写错了按白色处理。"""
    s = (color or "").strip().lstrip("#")
    if len(s) != 6:
        s = "FFFFFF"
    try:
        v = int(s, 16)
    except ValueError:
        v = 0xFFFFFF
    return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF


def _ass_color(r: int, g: int, b: int, alpha: int = 0) -> str:
    """ASS 颜色是 &HAABBGGRR（和常见的 #RRGGBB 反着来）。"""
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _parse_cues(path: Path | str) -> list[dict]:
    """读一份 SRT：返回 [{start_ms, end_ms, text}]（文本已压成一行）"""
    try:
        srt = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for m in _PAT.finditer(srt):
        st = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000 + int(m.group(4))
        en = (int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))) * 1000 + int(m.group(8))
        out.append({"start": st, "end": en,
                    "text": re.sub(r"\s+", " ", m.group(9)).strip()})
    return out


def _pair_texts(main_cues: list[dict], src_cues: list[dict]) -> list[str]:
    """把每条译文配上对应的原文。

    行数一样就按下标配（引擎产出的 `-raw.srt` 和成品逐行对齐，实测 36 行误差 0.000 秒）；
    行数对不上（用户导入字幕 / 引擎某些版本会合并断句）时按开始时间就近配，最多差 2 秒，
    配不上就只显示译文 —— 宁可少一行原文，也不能把不相干的句子顶上去。
    """
    if not src_cues:
        return [""] * len(main_cues)
    if len(main_cues) == len(src_cues):
        return [c["text"] for c in src_cues]
    out: list[str] = []
    j = 0                                        # 两份字幕的时间都是单调递增的，走双指针
    for c in main_cues:
        while (j + 1 < len(src_cues)
               and abs(src_cues[j + 1]["start"] - c["start"])
               <= abs(src_cues[j]["start"] - c["start"])):
            j += 1
        if j < len(src_cues) and abs(src_cues[j]["start"] - c["start"]) <= 2000:
            out.append(src_cues[j]["text"])
            j += 1
        else:
            out.append("")
    return out


def _ms_ts(ms: int) -> str:
    h, ms = divmod(int(ms), 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_bilingual_srt(main_srt: Path | str, src_srt: Path | str,
                        out_srt: Path | str, src_order: str = "src_top") -> int:
    """把「译文 SRT + 原文 SRT」合成一份双语 SRT，给人工校对用。

    ★ 2026-10-21 客户需求：批量翻译生肉后，想要一份带原文的字幕文件，方便用
      外部编辑器对着原文改译文。**只多写一个文件**，输出目录里那份纯中文 `.srt`
      一个字节都不动（配音和烧录都读它，混进原文会念错）。
    原文放上还是放下跟「双语字幕」设置走，默认原文在上。返回写出的条数。
    """
    main = _parse_cues(main_srt)
    texts = _pair_texts(main, _parse_cues(src_srt))
    src_top = (src_order or "src_top").strip().lower() != "src_bottom"
    blocks: list[str] = []
    for i, c in enumerate(main, 1):
        src = texts[i - 1] if i - 1 < len(texts) else ""
        if src:
            lines = [src, c["text"]] if src_top else [c["text"], src]
        else:
            lines = [c["text"]]
        blocks.append(f"{i}\n{_ms_ts(c['start'])} --> {_ms_ts(c['end'])}\n"
                      + "\n".join(lines) + "\n")
    Path(out_srt).write_text("\n".join(blocks), encoding="utf-8")
    return len(blocks)


def _two_line(src: str, main: str, size: float, play_w: int, spacing: float,
              scale: float, src_top: bool) -> str:
    """双语字幕：两行塞进同一条 Dialogue（ASS 用 \\N 硬换行）。

    每行各自按画面宽度算字号；原文那行默认是主字号的 80%（可在设置里调）。
    """
    src_base = max(24.0, size * max(30.0, min(100.0, scale)) / 100.0)
    src_fs = _fit_size(src, src_base, play_w, spacing=spacing)
    main_fs = _fit_size(main, size, play_w, spacing=spacing)
    pairs = [(src, src_fs), (main, main_fs)] if src_top else [(main, main_fs), (src, src_fs)]
    parts = []
    for text, fs in pairs:
        prefix = f"{{\\fs{fs:g}}}" if abs(fs - size) > 0.01 else ""
        parts.append(prefix + text)
    return "\\N".join(parts)


def srt_to_ass(srt_path: Path | str, ass_path: Path | str, font: str = "Microsoft YaHei",
               size: float = 42, margin_v: int = 30, outline: float = 2.2,
               play_res: str = "1280x720", spacing: float = 0.0,
               color: str = "#FFFFFF",
               bilingual: bool = False, src_srt: str = "",
               src_order: str = "src_top", src_scale: float = 80,
               outline_color: str = "") -> int:
    """转换并写出 ASS，返回字幕条数。

    bilingual=True 且 src_srt 可读时，每条字幕变成两行（原文 + 译文）；
    原文放上面还是下面由 src_order 决定（src_top / src_bottom），
    原文字号是主字号的 src_scale%（默认 80）。

    outline_color 留空 = 自动（浅色字幕配黑描边、深色字幕配白描边）；
    填 #RRGGBB 就用用户选的颜色。
    """
    w, h = play_res.split("x")
    play_w = int(w)
    srt = Path(srt_path).read_text(encoding="utf-8-sig")

    def t(a, b, c, d):  # SRT 时间戳 -> ASS 时间戳
        return f"{int(a)}:{b}:{c}.{int(d)//10:02d}"

    r, g, b = _rgb(color)
    primary = _ass_color(r, g, b)
    # 描边颜色：默认自动（深色字幕配白描边，浅色字幕配黑描边），
    # 用户在设置里指定了就用指定的。
    oc = (outline_color or "").strip()
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", oc):
        outline_color_ass = _ass_color(*_rgb(oc))
    else:
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
        outline_color_ass = _ass_color(255, 255, 255) if lum < 0.35 else _ass_color(0, 0, 0)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size:g},{primary},&H000000FF,{outline_color_ass},&H80000000,-1,0,0,0,100,100,{spacing:g},0,1,{outline},1.2,2,20,20,{margin_v},134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # ---- 主字幕先解析出来（双语要拿它和原文按行/按时间配对）----
    try:
        main_cues = []
        for m in _PAT.finditer(srt):
            st = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000 + int(m.group(4))
            en = (int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))) * 1000 + int(m.group(8))
            main_cues.append({"start": st, "end": en,
                              "text": re.sub(r"\s+", " ", m.group(9)).strip()})
    except Exception:  # noqa: BLE001 - 解析失败就走原来的单语路径，别把烧录搞挂
        main_cues = []

    src_texts: list[str] = []
    if bilingual and src_srt:
        src_texts = _pair_texts(main_cues, _parse_cues(src_srt)) if main_cues else []
    src_top = (src_order or "src_top").strip().lower() != "src_bottom"

    lines = [head.rstrip("\n")]
    n = 0
    if main_cues:
        for c in main_cues:
            st = f"{c['start'] // 3600000}:{(c['start'] // 60000) % 60:02d}:{(c['start'] // 1000) % 60:02d}.{(c['start'] % 1000) // 10:02d}"
            en = f"{c['end'] // 3600000}:{(c['end'] // 60000) % 60:02d}:{(c['end'] // 1000) % 60:02d}.{(c['end'] % 1000) // 10:02d}"
            text = c["text"]
            src = src_texts[n] if n < len(src_texts) else ""
            if bilingual and src:
                body = _two_line(src, text, size, play_w, spacing, src_scale, src_top)
            else:
                # 强制一行：把 SRT 里的换行/多空格压成单空格；太宽就按画面宽度缩字号
                fs = _fit_size(text, size, play_w, spacing=spacing)
                body = (f"{{\\fs{fs:g}}}" if fs < size else "") + text
            lines.append(f"Dialogue: 0,{st},{en},Default,,0,0,0,,{body}")
            n += 1
    else:
        # 兼容旧的解析路径（例如 SRT 里序号格式不标准）：按原逻辑再走一遍
        for m in _PAT.finditer(srt):
            st = t(m.group(1), m.group(2), m.group(3), m.group(4))
            en = t(m.group(5), m.group(6), m.group(7), m.group(8))
            text = re.sub(r"\s+", " ", m.group(9)).strip()
            fs = _fit_size(text, size, play_w, spacing=spacing)
            prefix = f"{{\\fs{fs:g}}}" if fs < size else ""
            lines.append(f"Dialogue: 0,{st},{en},Default,,0,0,0,,{prefix}{text}")
            n += 1
    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("srt")
    ap.add_argument("ass")
    ap.add_argument("--font", default="Microsoft YaHei")
    ap.add_argument("--size", type=float, default=42)
    ap.add_argument("--margin-v", type=int, default=30)
    ap.add_argument("--outline", type=float, default=2.2)
    ap.add_argument("--spacing", type=float, default=0.0)
    ap.add_argument("--color", default="#FFFFFF")
    ap.add_argument("--play-res", default="1280x720")
    args = ap.parse_args()
    n = srt_to_ass(args.srt, args.ass, args.font, args.size, args.margin_v,
                   args.outline, args.play_res, spacing=args.spacing, color=args.color)
    print(f"ASS 写出: {args.ass} ({n} 条)")


if __name__ == "__main__":
    main()
