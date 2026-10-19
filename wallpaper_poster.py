#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从视频壁纸里抽一帧，给窗口缩放时当遮罩。

Windows 在窗口最大化/还原时会重建 MediaPlayerElement 的视频表面，偶尔露出绿帧/黑帧；
更糟的是播放器重建失败会退回内置星空底图。前端在尺寸变化时先把这张静帧盖上去，
等视频表面稳定再撤掉，用户看到的就是同一张壁纸的静止画面。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import tools
from job import no_window_kwargs


def _out_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    d = base / "StarryVT-WinUI" / "posters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def render(path: str, at: float = 1.0) -> Path:
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    st = src.stat()
    tag = hashlib.sha1(
        f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|{at:g}".encode("utf-8")
    ).hexdigest()[:16]
    out = _out_dir() / f"poster-{tag}.png"
    if out.is_file() and out.stat().st_size > 1024:
        return out

    cmd = [tools.path("ffmpeg"), "-v", "error", "-y",
           "-ss", f"{max(0.0, at):g}", "-i", str(src),
           "-frames:v", "1", "-vf", "scale=1280:-2", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **no_window_kwargs())
    if r.returncode != 0 or not out.is_file() or out.stat().st_size <= 1024:
        raise RuntimeError(f"抽帧失败: {(r.stderr or '')[-300:]}")

    # 只留最近 12 张，别让收藏的壁纸越攒越多
    try:
        for old in sorted(_out_dir().glob("poster-*.png"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[12:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return out
