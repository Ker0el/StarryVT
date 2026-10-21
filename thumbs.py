#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""视频缩略图：批量列表里那一格小图（有内嵌封面就用封面，没有就抽第一帧）。

为什么单独一个模块：抽帧要起 ffmpeg（每个几百毫秒），**绝不能拖慢扫描** ——
扫描先把清单给界面，缩略图由界面按行**异步**来要，谁要谁等。
抽出来的图按「路径 + 修改时间 + 大小」缓存，重扫同一个文件夹就不用再抽了。
"""
from __future__ import annotations

import hashlib
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import diag
import tools
from job import no_window_kwargs

def cache_dir() -> Path:
    """缩略图缓存目录（跟着「数据目录」走，见 paths.py）"""
    import paths
    return paths.resolve("thumbs")

WIDTH = 176          # 列表里那一格就这么大，别抽原尺寸（几百 KB 一张没必要）

# 网络封面（YouTube 的 i.ytimg.com 之类）要自己带 UA，有些图床直接拒 python-urllib
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _url_key(url: str) -> str:
    return "u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def fetch_many(urls, params: dict | None = None, workers: int = 6) -> dict[str, str]:
    """把网络封面图下到本地缓存，返回 {原地址: 本地路径}

    为什么要绕一道：界面里的 BitmapImage 是 WinRT 自己去下图的，**不走我们设置里的代理** ——
    YouTube 的图在国内根本下不来，卡片上就只剩占位图。这里走后端（带代理和 UA）拿下来，
    界面读本地文件，稳定且离线也能看第二次。
    """
    items = [u for u in dict.fromkeys(urls or []) if isinstance(u, str) and u.startswith("http")]
    if not items:
        return {}
    import mirrors
    opener = mirrors.opener(params or {})
    cache = cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    def one(url: str) -> tuple[str, str]:
        dest = cache / f"{_url_key(url)}.jpg"
        try:
            if dest.is_file() and dest.stat().st_size > 0:
                return url, str(dest)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=15) as r:
                data = r.read(3 * 1024 * 1024)
            if len(data) < 500:
                return url, ""
            dest.write_bytes(data)
            return url, str(dest)
        except Exception:  # noqa: BLE001 - 下不到封面只是没有封面，不影响功能
            return url, ""

    out: dict[str, str] = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
            for url, path in ex.map(one, items):
                if path:
                    out[url] = path
    except Exception as e:  # noqa: BLE001
        diag.write(f"[thumb] 封面批量下载失败: {type(e).__name__}: {e}", "run")
    return out


def _key(video: Path) -> str:
    try:
        st = video.stat()
        raw = f"{video.resolve()}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        raw = str(video)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def get(video: Path | str, at: float = 3.0) -> Path | None:
    """返回缩略图路径；抽不出来返回 None（界面上就显示占位图，不是错误）"""
    video = Path(video)
    if not video.is_file():
        return None
    cache = cache_dir()
    dest = cache / f"{_key(video)}.jpg"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    cache.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.jpg")
    ff = tools.path("ffmpeg")

    # 1) 有内嵌封面（很多课程视频带一张封面图）就用它 —— 比第一帧有代表性
    for args in (
        ["-map", "0:v", "-map", "-0:V", "-frames:v", "1"],     # attached_pic
        ["-ss", "12", "-frames:v", "1"],       # 片头 logo 通常在前十秒，往后取更像"内容"
        ["-ss", f"{at:.2f}", "-frames:v", "1"], # 短片子往中间取
        ["-frames:v", "1"],                     # 实在不行就第一帧
    ):
        try:
            r = subprocess.run(
                [ff, "-v", "error", "-y", "-i", str(video), *args,
                 "-vf", f"scale={WIDTH}:-2", "-q:v", "5", str(tmp)],
                capture_output=True, text=True, timeout=40, **no_window_kwargs())
        except Exception:  # noqa: BLE001 - 超时/找不到 ffmpeg 都只是"没有缩略图"
            continue
        # ★ 判据是 ffmpeg 的退出码，不是文件大小：画面单一的那一帧
        #   （淡入 / 纯色 / 暗场）压出来可能只有 300 多字节，按 >500 判会把
        #   一张**合法**的缩略图丢掉（2026-10-15 实测踩过，见项目记忆）。
        if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return dest
    diag.write(f"[thumb] 抽不出缩略图: {video.name}", "run")
    return None
