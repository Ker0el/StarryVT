#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载随包分发的外部程序到 .tools/bin/（打包前跑一次；.tools/ 不入 git）

    python fetch_tools.py [--proxy http://127.0.0.1:7897]

抓两样东西：
  1. FFmpeg（BtbN 的 win64 **LGPL** 共享库构建）—— ffmpeg.exe / ffprobe.exe + av*.dll
  2. yt-dlp 独立可执行文件（官方 release 的 yt-dlp.exe）

为什么用 LGPL 构建而不用最常见的 GPL 构建：
  本软件闭源商用。GPL 构建（带 x264/x265）随包分发要一并履行 GPL 的源码提供义务；
  LGPL 构建只要求附许可证、允许替换库文件——发布时干净得多。
  实测 LGPL 构建已包含本软件用到的全部能力：libsvtav1 编码、libass 烧字幕、
  adelay/atempo/amix 滤镜、aac 编码、ffprobe。

抓完会自检能力，缺 libsvtav1 或 ass 会直接报错（上游构建配置变了要及时发现）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import mirrors

ROOT = Path(__file__).parent
BIN_DIR = ROOT / ".tools" / "bin"

# 用 **稳定分支** 的 LGPL 共享库构建，不要用 master 每日构建：
# 2026-09-27 实测 BtbN 的 ffmpeg-master-latest（N-126889）有音频滤镜回归——
# adelay+amix、anullsrc+concat 这类普通图会卡死在 time≈0.97s 不再出帧（换成 9.0 稳定版全过）。
# 这种"偶发卡住"最难查，所以钉住 n9.0 分支（同样是 LGPL，商用许可要求不变）。
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip")
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"

# 共享库构建里除了 ffmpeg/ffprobe 之外要带的 DLL 前缀（ffplay 不需要，省 18MB）
DLL_PREFIXES = ("av", "sw")


def _opener(proxy: str):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # 空 ProxyHandler = 真正的直连（否则会读系统代理，代理关掉后一律 10061）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _download(opener, url: str, dest: Path) -> None:
    print(f"下载 {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with opener.open(url, timeout=300) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                print(f"\r  {got / 1048576:6.1f} / {total / 1048576:.1f} MB", end="")
    print(f"\r  完成 {got / 1048576:.1f} MB" + " " * 20)


def fetch_ffmpeg(opener, tmp: Path) -> None:
    zpath = tmp / "ffmpeg.zip"
    _download(opener, FFMPEG_URL, zpath)
    with zipfile.ZipFile(zpath) as z:
        members = [n for n in z.namelist() if "/bin/" in n and not n.endswith("/")]
        keep = [n for n in members
                if Path(n).name.startswith(DLL_PREFIXES) or Path(n).name in ("ffmpeg.exe", "ffprobe.exe")]
        if not any(n.endswith("ffmpeg.exe") for n in keep):
            raise SystemExit("!! 压缩包里没找到 ffmpeg.exe（上游下载地址可能变了）")
        for n in keep:
            with z.open(n) as src, open(BIN_DIR / Path(n).name, "wb") as dst:
                dst.write(src.read())
            print(f"  + {Path(n).name}  ({z.getinfo(n).file_size / 1048576:.1f} MB)")


def fetch_ytdlp(opener) -> None:
    _download(opener, YTDLP_URL, BIN_DIR / "yt-dlp.exe")
    print("  + yt-dlp.exe")


def fetch_wheels(proxy: str) -> None:
    """随包分发「只放在 GitHub 上」的 Python 包（引擎装运行时要用，见 wheels.py）

    引擎的运行时依赖里，除了这一个包以外全部走清华 PyPI / 上海交大 / hf-mirror；
    只有它是 GitHub Releases 上的定制 wheel，没有代理的机器**必然**下不动。
    所以出包时抓一份放进 wheelhouse\\，装完软件就能离线装上运行时。

    版本要跟着引擎走：清单从本机已装好的引擎的 pylock 里读（hash 也是那儿的）。
    上游换版本后对不上时，软件端会自动退回加速镜像现下（见 wheels._local）。
    """
    import wheels

    files = wheels.pylocks()
    if not files:
        raise SystemExit("!! 本机没有装好的 finesub 引擎，读不到依赖清单\n"
                         "   先在本机装一次引擎（软件里点「一键安装引擎」），再打包")
    out = ROOT / "wheelhouse"
    out.mkdir(parents=True, exist_ok=True)
    got = 0
    for f in files:
        for url, sha in wheels.entries(f):
            name = wheels._name_of(url)
            dest = out / name
            if wheels._ok(dest, sha):
                print(f"  已经是这一份，跳过: {name}")
                continue
            print(f"下载 {name}（{sha[:8]}…）")
            mirrors.fetch(url, dest, {"github_mirror": "auto", "proxy": proxy},
                          report=lambda s, p, t: print(f"  {t}"), sha256=sha)
            got += 1
    total = sum(f.stat().st_size for f in out.iterdir())
    print(f"  {out}  共 {len(list(out.iterdir()))} 个文件 / {total / 1048576:.0f} MB"
          + (f"（新下 {got} 个）" if got else ""))


def verify() -> None:
    """自检：确认 ffmpeg 具备本软件需要的能力"""
    ff = BIN_DIR / "ffmpeg.exe"
    print("自检 ffmpeg 能力…")

    def has(args: list[str], needle: str) -> bool:
        out = subprocess.run([str(ff), "-hide_banner", *args],
                             capture_output=True, text=True).stdout
        return needle in out

    ver = subprocess.run([str(ff), "-hide_banner", "-version"],
                         capture_output=True, text=True).stdout.splitlines()
    print("  " + (ver[0] if ver else "?"))
    checks = [(["-encoders"], "libsvtav1"), (["-encoders"], " aac "),
              (["-filters"], " ass "), (["-filters"], "adelay"), (["-filters"], "amix")]
    bad = [needle for args, needle in checks if not has(args, needle)]
    if bad:
        raise SystemExit(f"!! 这个 ffmpeg 构建缺少本软件需要的组件：{bad}\n"
                         f"   请换一个构建（需要有 libsvtav1 + libass 的版本）")
    print("  OK：libsvtav1 / libass / adelay / amix / aac 齐全")

    yt = BIN_DIR / "yt-dlp.exe"
    if yt.exists():
        v = subprocess.run([str(yt), "--version"], capture_output=True, text=True).stdout.strip()
        print(f"  yt-dlp 版本: {v}")


def fetch_engine_wheels(proxy: str) -> None:
    """随包分发翻译引擎本体和它的 Python 依赖（engine_whl\\）

    引擎是 GPL-3.0 的纯 Python wheel，原样随包（未修改，wheel 里就带源码），
    再配齐它的依赖，装引擎时先用本地 wheelhouse，失败再走网络。这样没有代理、
    GitHub 镜像又抽风的机器也能把引擎装上（运行时和模型仍然首次下载）。

    版本范围跟 `engine.ENGINE_REQ` 走；换引擎版本时两边一起改。
    """
    import engine
    out = ROOT / "engine_whl"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.whl"):
        old.unlink()
    cmd = [sys.executable, "-m", "pip", "download", engine.ENGINE_REQ,
           "--dest", str(out), "--only-binary=:all:",
           "--python-version", "3.12", "--platform", "win_amd64", "--implementation", "cp",
           "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"]
    if proxy:
        cmd += ["--proxy", proxy]
    print(f"下载 {engine.ENGINE_REQ} + 依赖 …")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("!! 引擎 wheelhouse 下载失败：\n" + (r.stderr or r.stdout or "")[-800:])
    wheels = sorted(out.glob("*.whl"))
    if not any(w.name.startswith("finesub-") for w in wheels):
        raise SystemExit("!! 下载完没找到 finesub-*.whl")
    print(f"  {out}  {len(wheels)} 个包 / {sum(w.stat().st_size for w in wheels) / 1048576:.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="", help="如 http://127.0.0.1:7897（国内访问 GitHub 用）")
    args = ap.parse_args()

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / ".tools" / "dl"
    tmp.mkdir(parents=True, exist_ok=True)
    opener = _opener(args.proxy)
    if args.proxy:                      # mirrors.fetch 用普通 urlopen，让它也认这个代理
        urllib.request.install_opener(opener)

    print("=== FFmpeg（LGPL 构建）===")
    fetch_ffmpeg(opener, tmp)
    print("=== yt-dlp ===")
    fetch_ytdlp(opener)
    print("=== 只放在 GitHub 上的 Python 包（随包分发）===")
    fetch_wheels(args.proxy)
    print("=== 翻译引擎本体 + 依赖（随包分发，离线装引擎）===")
    fetch_engine_wheels(args.proxy)

    # 装完不留下载缓存，避免 .tools 里留一份重复的百兆压缩包
    for f in tmp.glob("*.zip"):
        f.unlink()
    try:
        tmp.rmdir()
    except OSError:
        pass

    verify()
    total = sum(f.stat().st_size for f in BIN_DIR.iterdir())
    print(f"\n[OK] {BIN_DIR}  共 {len(list(BIN_DIR.iterdir()))} 个文件 / {total / 1048576:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
