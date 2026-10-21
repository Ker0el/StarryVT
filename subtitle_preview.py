#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕样式实时预览：用真实的 srt2ass + libass 渲一张 1280x720 的 PNG。

设置页改字体/字号/描边/底部距离时，前端调 RPC `subtitle_preview`，
后端用和最终烧录**同一条** srt2ass + ffmpeg 路径渲一帧，返回 PNG 路径。
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import srt2ass
import subtitle_fonts
import tools
from job import no_window_kwargs

SAMPLE = "星译 Easy Translate 字幕预览 Subtitle Preview"
#: 双语预览时「原文」那一行（假想的外语原文）
SAMPLE_SRC = "Easy Translate - Subtitle Preview"


def render(style: dict, large: bool = False) -> Path:
    font = str(style.get("font") or "Starry Han Sans Light")
    try:
        size = float(style.get("size") or 42)
    except (TypeError, ValueError):
        size = 42.0
    try:
        outline = float(style.get("outline") or 2.2)
    except (TypeError, ValueError):
        outline = 2.2
    try:
        margin_v = int(style.get("margin_v") or 30)
    except (TypeError, ValueError):
        margin_v = 30
    try:
        spacing = float(style.get("spacing") or 0)
    except (TypeError, ValueError):
        spacing = 0.0
    color = str(style.get("color") or "#FFFFFF").strip() or "#FFFFFF"
    outline_color = str(style.get("outline_color") or "").strip()
    # 字体：用户选了字体文件就用它（族名从文件里读），否则用随包的思源黑体
    font, font_file = subtitle_fonts.resolve(
        str(style.get("font") or ""), str(style.get("font_file") or ""))
    bilingual = bool(style.get("bilingual"))
    src_order = str(style.get("src_order") or "src_top")
    try:
        src_scale = float(style.get("src_scale") or 80)
    except (TypeError, ValueError):
        src_scale = 80.0

    import paths
    root = paths.resolve("subtitle_preview")
    root.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(
        f"{font}\x00{size}\x00{outline}\x00{margin_v}\x00{spacing}\x00{color}\x00{font_file}\x00{bool(large)}"
        f"\x00{bilingual}\x00{src_order}\x00{src_scale}\x00{outline_color}".encode("utf-8")
    ).hexdigest()[:12]
    srt = root / f"{tag}.srt"
    ass = root / f"{tag}.ass"
    png = root / f"{tag}.png"

    srt.write_text(f"1\n00:00:00,000 --> 00:00:05,000\n{SAMPLE}\n", encoding="utf-8")
    src = root / f"{tag}-src.srt"
    if bilingual:
        src.write_text(f"1\n00:00:00,000 --> 00:00:05,000\n{SAMPLE_SRC}\n", encoding="utf-8")
    srt2ass.srt_to_ass(srt, ass, font=font, size=size, margin_v=margin_v,
                       outline=outline, spacing=spacing, color=color,
                       play_res="1280x720",
                       bilingual=bilingual, src_srt=str(src) if bilingual else "",
                       src_order=src_order, src_scale=src_scale,
                       outline_color=outline_color)

    vf = f"ass={ass.name}"
    if font_file and Path(font_file).is_file():
        # 单一字体目录，避免每改一次样式就多复制一份 8MB 字体
        font_dir = root / ".fonts"
        font_dir.mkdir(parents=True, exist_ok=True)
        dest = font_dir / Path(font_file).name
        try:
            if not dest.is_file() or dest.stat().st_size != Path(font_file).stat().st_size:
                shutil.copyfile(font_file, dest)
            vf += ":fontsdir=.fonts"
        except OSError:
            pass

    cmd = [tools.path("ffmpeg"), "-v", "error", "-y",
           # 纯黑样片背景：字幕颜色和描边看得最清楚
           "-f", "lavfi", "-i", f"color=c=0x000000:s={'1920x1080' if large else '1280x720'}:d=1",
           "-vf", vf, "-s", "1920x1080" if large else "640x360",
           "-frames:v", "1", str(png)]
    r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **no_window_kwargs())
    if r.returncode != 0 or not png.is_file():
        raise RuntimeError(f"预览渲染失败: {(r.stderr or '')[-300:]}")

    # 只留最近 8 张预览图
    try:
        for old in sorted(root.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[8:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return png
