#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""热更新：检查新版本 → 下载安装包 → 静默安装 → 自动重启

更新源：GitHub Releases 上的 latest.json（公开仓库，无需令牌）。
    {"version": "0.2.0", "url": ".../StarryVT-Setup.exe", "notes": "...", "published": "..."}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import diag
import tools
from job import CREATE_BREAKAWAY_FROM_JOB, no_window_kwargs

DEFAULT_UPDATE_URL = "https://github.com/Ker0el/StarryVT/releases/latest/download/latest.json"

# 2026-10-17 用户决定：**只用 GitHub，不再维护国内对象存储**。
# 这里保留空元组，是为了让「用户设置里的自定义更新地址」仍然优先（见 _manifest_urls）。
SELF_HOSTED_UPDATE_URLS: tuple[str, ...] = ()


def _manifest_urls(url: str) -> list[str]:
    """要依次试的更新清单地址：用户自定义 > GitHub Releases。

    ⚠️ 设置里那个「更新地址」的默认值就是 GitHub 地址，而 cfg.save()
    会把整份配置写回文件 —— 凡是**等于默认值**的，一律当作「没填」，
    避免把代码里的更新源短路掉。
    """
    u = (url or "").strip().rstrip("/")
    if u and u != DEFAULT_UPDATE_URL.rstrip("/"):
        return [u]
    out = [s for s in SELF_HOSTED_UPDATE_URLS if s.strip()]
    out.append(DEFAULT_UPDATE_URL)
    return out


def _opener(proxy: str = ""):
    proxy = (proxy or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _is_self_hosted(u: str) -> bool:
    """这个地址是不是我们自己维护的国内源（现在已不使用，保留给自定义地址判断）。"""
    host = urlsplit(u).netloc.lower()
    return bool(host) and any(
        urlsplit(s).netloc.lower() == host for s in SELF_HOSTED_UPDATE_URLS if s.strip())


def _opener_for(u: str, proxy: str = ""):
    """按地址选 opener：自建源直连（如果以后再加），其余走代理。"""
    if _is_self_hosted(u):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener(proxy)


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def check(current: str, url: str = "", proxy: str = "") -> dict:
    """检查是否有新版本。返回 {ok, has_update, version, url, notes, current, error}

    latest.json 里的 sha256 会被透传给下载步骤做完整性校验（老版本清单没有这个字段也没关系）。
    """
    last_kind = "未知错误"
    tried: list[str] = []
    for u in _manifest_urls(url):
        tried.append(u)
        try:
            with _opener_for(u, proxy).open(u, timeout=20) as r:
                info = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 这个源不行就换下一个
            last_kind = type(e).__name__
            diag.write(f"检查更新：源 {u} 连不上（{type(e).__name__}: {e}）", "update")
            continue
        latest = str(info.get("version") or "").strip()
        pkg = str(info.get("url") or "").strip()
        if not latest or not pkg:
            last_kind = "清单格式不正确"
            diag.write(f"检查更新：源 {u} 返回的清单缺版本号或下载地址", "update")
            continue
        has = _vtuple(latest) > _vtuple(current)
        diag.write(f"检查更新：当前 v{current}，最新 v{latest}，有更新={has}"
                   f"（源 {u}，共试了 {len(tried)} 个）", "update")
        return {"ok": True, "has_update": has, "version": latest, "current": current,
                "pkg_url": pkg, "notes": str(info.get("notes") or ""),
                "sha256": str(info.get("sha256") or "").strip().lower(),
                "published": str(info.get("published") or ""), "source": u}

    # ★ 每个源失败都写日志：以前只在成功时写，于是客户报「检测不到最新版」时，
    #   诊断报告里一条记录都没有 —— 分不清是「没新版」还是「压根没查成」。
    diag.write(f"检查更新失败（当前 v{current}）：试了 {len(tried)} 个源都不行，"
               f"最后一个是 {last_kind}", "update")
    return {"ok": False, "error": f"检查更新失败（{last_kind}）——检查网络/代理"}


def download(pkg_url: str, proxy: str, job, report, sha256: str = "") -> Path:
    """下载安装包到临时目录（支持取消与进度）

    下完必须校验：大小对不上（服务端给了 Content-Length）或 sha256 不符，就当场失败。
    半个安装包跑静默安装 = 更新装坏、程序再也起不来——这种事故比"下载失败"难查得多。
    """
    dst = Path(tempfile.gettempdir()) / "StarryVT-Setup-new.exe"
    if dst.exists():
        try:
            dst.unlink()
        except Exception:
            pass
    report("update", 0, "正在下载新版本…")
    with _opener_for(pkg_url, proxy).open(pkg_url, timeout=60) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            job.check()
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                report("update", int(got * 100 / total),
                       f"下载中 {got // 1048576}MB / {total // 1048576}MB")
    size = dst.stat().st_size
    if size < 1024 * 1024:
        raise RuntimeError("下载的安装包不完整（文件太小），请稍后重试")
    if total and size != total:
        raise RuntimeError(f"下载的安装包不完整（{size // 1048576}MB / 应为 {total // 1048576}MB），"
                           f"请检查网络后重试")
    if sha256:
        report("update", None, "校验安装包…")
        if _sha256(dst) != sha256:
            dst.unlink(missing_ok=True)
            raise RuntimeError("安装包校验失败（下载过程中被改动或损坏），请重试")
    diag.write(f"新版本已下载: {dst}（{size // 1048576}MB）", "update")
    return dst


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_script(setup_path: Path, exe: str, pid: int, restart: bool, also_name: str = "") -> str:
    """生成更新脚本（纯 ASCII + CRLF：cmd.exe 按控制台代码页读批处理，
    非 ASCII 注释会变乱码、LF 换行还会错行——发动机那边踩过同样的坑）

    等主程序退出的判断用 tasklist + for /f 精确比 PID：
    不用 find/findstr —— 它们的名字在装了 Git/Cygwin 的机器上可能被同名 Unix 工具顶掉
    （实测：Git 的 find 让判断恒为"已退出"，等于没等）；也不用 timeout —— 它在没有
    控制台的进程里会直接失败（v1.0 更新后没自动打开，八成就是"没等到程序退出就开装"）。

    全程写日志到软件自己的 logs\\update.log：客户说"更新完没打开"时，这份时间线就是证据。
    """
    logfile = "%APPDATA%\\StarryVT\\logs\\update.log"
    echo = f'echo [%TIME%] '
    # 装回哪儿：**正在运行的这个 exe 在哪，就装回哪**。
    # 不能靠安装包的 DefaultDirName（它写的是 {src}\StarryVT，而 {src} = 安装包自己
    # 所在的目录）——更新包是下到 %TEMP% 再跑的，那样会装进 %TEMP%\StarryVT。
    # 也不能靠 Inno 的 UsePreviousAppDir：它读的是注册表里"上次安装目录"，一旦那次
    # 记录被写成临时目录，之后每次更新都会跟着错下去（v1.6→v1.7 实际踩到：
    # 用户界面还是 1.6，%TEMP% 下却多出一份 1.7）。
    app_dir = str(Path(exe).parent)
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        f'if not exist "%APPDATA%\\StarryVT\\logs" mkdir "%APPDATA%\\StarryVT\\logs"',
        f'{echo}update script started (pid={pid})>> "{logfile}"',
        "rem Wait for the running app (our PID) to exit, up to ~60s, then force-kill.",
        "set /a WAIT=0",
        ":wait",
        "set ALIVE=",
        f'for /f "tokens=2" %%p in (\'tasklist /nh /fi "PID eq {pid}" 2^>nul\') do '
        f'if "%%p"=="{pid}" set ALIVE=1',
        "if not defined ALIVE goto gone",
        "set /a WAIT+=1",
        "if !WAIT! GEQ 60 goto kill",
        "ping -n 2 -w 500 127.0.0.1 >nul",
        "goto wait",
        ":kill",
        f'{echo}app still alive after 60s, killing>> "{logfile}"',
        f"taskkill /f /pid {pid} >nul 2>nul",
        ":gone",
        f'{echo}app exited, installing>> "{logfile}"',
        # 3.0 的界面和我们（后端）是两个进程：界面退出了我们可能还挂着，
        # 占着 backend\StarryVT.exe，安装程序就替换不掉 —— 按镜像名补一刀。
        (f'taskkill /f /im "{also_name}" >nul 2>nul' if also_name
         else "rem no extra process to kill"),
        "rem Silent install. `start /wait` makes sure we do not touch anything until",
        "rem the installer has really finished.",
        "rem /DIR= 是必须的：见上面 app_dir 那段说明。",
        f'start "" /wait "{setup_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART '
        f'/CLOSEAPPLICATIONS /DIR="{app_dir}"',
        f'{echo}installer finished (errorlevel !ERRORLEVEL!)>> "{logfile}"',
    ]
    if restart:
        lines.append(f'start "" "{exe}"')
        lines.append(f'{echo}app restarted>> "{logfile}"')
    lines.append("endlocal")
    return "\r\n".join(lines) + "\r\n"


def install_and_restart(setup_path: Path, restart: bool = True):
    """脱离本进程执行：等本程序退出 → 静默安装 → 重新启动

    必须用独立脚本，不能拼一条 cmd 命令串（v1.0/v1.1 就是这么写的，实测出了两个坑）：
      1. `timeout /t 2` 在无控制台的进程里直接失败 → 等于没有等待；
      2. cmd 不等 GUI 程序（安装程序就是），`a.exe & b` 会立刻执行 b
         → 安装还没写完就把程序拉起来了，拉起来的是旧 exe，还顺手锁住了自己，
         安装程序替换失败 → 用户看到"更新完没自动打开 / 版本没变"。
    """
    exe = sys.executable
    also_name = ""
    # 3.0 起后端起在 backend\ 里，用户看到的是它上一层那个界面 exe：
    # 重启要重启**界面** —— 把后端自己拉起来只会多一个没窗口的进程。
    # （界面退出时会顺手收掉后端；脚本里再按镜像名补一刀，防它赖着不走锁住文件）
    _parent = Path(exe).parent
    if _parent.name.lower() == "backend":
        _front = _parent.parent / "StarryVt.exe"
        if _front.is_file():
            exe, also_name = str(_front), "StarryVt.exe"
    if not tools.is_packaged():
        # 开发模式：只打开安装包，不自动装/重启
        # ⚠️⚠️ 这里的判据**必须**用 tools.is_packaged()，不能用 getattr(sys, "frozen", False)：
        #   生产版后端是 Nuitka 编的，而 **Nuitka 不设 sys.frozen** —— 用旧写法的话，
        #   用户在正式版上点「立即更新」会掉进这个开发模式分支：安装包被当 GUI 程序打开
        #   （不静默安装）、不写更新脚本，然后进程自己退出 → 界面永远停在 100%。
        #   而开发版是 PyInstaller 编的（设 sys.frozen），本地测永远走对，完全测不出来。
        #   （2026-10-15 实测事故。）
        subprocess.Popen([str(setup_path)], **no_window_kwargs())
        return
    script = Path(tempfile.gettempdir()) / "starryvt-update.cmd"
    # 用系统 ANSI 代码页（中文 Windows 上是 GBK）写：cmd 就是按这个代码页读批处理的，
    # 用户把软件装在中文目录（D:\软件\StarryVT）时路径才不会变成乱码。
    script.write_text(
        _update_script(setup_path, exe, os.getpid(), restart, also_name),
        encoding="mbcs", errors="replace", newline="",
    )
    # ★ CREATE_BREAKAWAY_FROM_JOB 不能去掉：job.guard() 把本进程挂进了一个
    # 「本进程一退出就杀掉全部子孙」的作业对象（用来防止 finesub/uv 变孤儿占着锁）。
    # 这条更新脚本干的正是「等本进程退出」，不脱离的话它会和我们一起被杀掉——
    # 用户看到的就是"更新点了没反应 / 装完没自动打开"。
    # job.guard() 那边也对应地放开了 JOB_OBJECT_LIMIT_BREAKAWAY_OK，两处必须成对。
    subprocess.Popen(["cmd", "/c", str(script)],
                     creationflags=0x00000008 | 0x00000200       # DETACHED_PROCESS | NEW_PROCESS_GROUP
                                     | CREATE_BREAKAWAY_FROM_JOB,
                     close_fds=True)
    diag.write(f"已启动安装程序，本程序即将退出以完成更新（脚本 {script}）", "update")
