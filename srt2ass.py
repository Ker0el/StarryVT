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


def srt_to_ass(srt_path: Path | str, ass_path: Path | str, font: str = "Microsoft YaHei",
               size: float = 42, margin_v: int = 30, outline: float = 2.2,
               play_res: str = "1280x720", spacing: float = 0.0,
               color: str = "#FFFFFF") -> int:
    """转换并写出 ASS，返回字幕条数。"""
    w, h = play_res.split("x")
    play_w = int(w)
    srt = Path(srt_path).read_text(encoding="utf-8-sig")

    def t(a, b, c, d):  # SRT 时间戳 -> ASS 时间戳
        return f"{int(a)}:{b}:{c}.{int(d)//10:02d}"

    r, g, b = _rgb(color)
    primary = _ass_color(r, g, b)
    # 深色字幕配白描边，浅色字幕配黑描边，保证任何背景下都看得清
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    outline_color = _ass_color(255, 255, 255) if lum < 0.35 else _ass_color(0, 0, 0)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size:g},{primary},&H000000FF,{outline_color},&H80000000,-1,0,0,0,100,100,{spacing:g},0,1,{outline},1.2,2,20,20,{margin_v},134

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head.rstrip("\n")]
    n = 0
    for m in _PAT.finditer(srt):
        st = t(m.group(1), m.group(2), m.group(3), m.group(4))
        en = t(m.group(5), m.group(6), m.group(7), m.group(8))
        # 强制一行：把 SRT 里的换行/多空格压成单空格；太宽就按画面宽度缩字号
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
