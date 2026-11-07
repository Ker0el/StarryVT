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
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import diag
import mirrors
import tools
from job import CREATE_BREAKAWAY_FROM_JOB, Cancelled, no_window_kwargs

DEFAULT_UPDATE_URL = "https://github.com/Ker0el/StarryVT/releases/latest/download/latest.json"

# 2026-10-17 用户决定：**只用 GitHub，不再维护国内对象存储**。
# 这里保留空元组，是为了让「用户设置里的自定义更新地址」仍然优先（见 _manifest_urls）。
SELF_HOSTED_UPDATE_URLS: tuple[str, ...] = ()


def _with_mirrors(u: str) -> list[str]:
    """GitHub 地址再补几条 gh-proxy 镜像（直连优先，失败才用）。

    2026-10-29 实测：同一台机器 github.com 经代理约 2/3 的 TLS 握手超时，
    而 gh-proxy.org / gh-proxy.com 经代理稳定 200。检查和下载都走这条兜底。
    """
    out = [u]
    if "github.com/" in u:
        # 只保留实测稳的两条（hk.gh-proxy.com 2026-10-29 经代理超时）
        picks = [mirrors.CANDIDATES[0]]
        if len(mirrors.CANDIDATES) > 2:
            picks.append(mirrors.CANDIDATES[2])
        for prefix in picks:
            out.append(prefix.rstrip("/") + "/" + u)
    return out


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
    out.extend(_with_mirrors(DEFAULT_UPDATE_URL))
    return out


def _opener(proxy: str = ""):
    proxy = (proxy or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # 空 ProxyHandler = 真正的直连。不传的话 build_opener 会自己去读 Windows 系统代理，
    # 用户关掉代理软件后系统里还留着 127.0.0.1:7897 → WinError 10061（2026-10-21 客户报障）。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def last_update_problem(current: str) -> str:
    """上次更新没装到正在运行的这个程序上时，返回一句人话；正常返回 ""

    ★ 2026-11-05 客户报障：「更新下载完、程序退出，再打开还是提示更新」。
      更新脚本会把装之前/之后的 exe 尺寸+时间写进 logs\\update-last.log，
      开机时对一下：目标版本比当前新、或程序文件根本没变 —— 就是没换成。
      这句话会进 app 日志，客户下次导诊断报告就能直接看到。
    """
    p = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT" / "logs" / "update-last.log"
    try:
        if not p.is_file():
            return ""
        if time.time() - p.stat().st_mtime > 3 * 24 * 3600:
            return ""
        info = {}
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip().lower()] = v.strip().strip('"').strip()
    except OSError:
        return ""
    target = info.get("target", "")
    if not target or _vtuple(target) <= _vtuple(current):
        return ""
    same = info.get("before") and info.get("before") == info.get("after")
    where = info.get("exe") or info.get("dir") or "?"
    why = "安装程序跑完了但程序文件没变（被占用，或被安全软件拦了）" if same else \
          "安装程序把新版本装到了别处，你打开的这份没被替换"
    return (f"上次更新到 v{target} 没生效：{why}。\n"
            f"这次更新装的文件：{where}；当前运行的是 v{current}。\n"
            "请确认是通过哪个快捷方式/目录启动的；必要时手动运行安装包，"
            "并把安装目录选成你实际在用的那个。")


#: 从 GitHub Release 页面地址推出 API 地址（只有默认更新源才兜底，用户自定义地址不动）
_API_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/")


def _api_latest_url(u: str) -> str:
    m = _API_RE.match(u or "")
    if not m:
        return ""
    return f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases/latest"


def _info_from_api_release(rel: dict, current: str, src: str) -> dict | None:
    """把 GitHub API 的 release JSON 变成和 latest.json 一样的结构。

    ★ 2026-11-07 客户报障：v6.60 的 release 只传了 exe、漏了 latest.json，
      `releases/latest/download/latest.json` 全程 404，检查更新彻底失效。
      清单是我们自己发的、可以漏；release 本身才是唯一权威，所以 404 时改问 API。
    """
    tag = str(rel.get("tag_name") or rel.get("name") or "").strip()
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    pkg = ""
    sha = ""
    for a in rel.get("assets") or []:
        name = str(a.get("name") or "")
        low = name.lower()
        if not (low.endswith(".exe") and "setup" in low):
            continue
        pkg = str(a.get("browser_download_url") or "")
        digest = str(a.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            sha = digest.split(":", 1)[1].strip().lower()
        break
    if not version or not pkg:
        return None
    notes = re.sub(r"\s+", " ", str(rel.get("body") or "")).strip()[:800]
    return {"ok": True, "has_update": _vtuple(version) > _vtuple(current),
            "version": version, "current": current, "pkg_url": pkg, "notes": notes,
            "sha256": sha, "published": str(rel.get("published_at") or "")[:10],
            "source": src, "network": False}


def _api_fallback(u: str, proxy: str, current: str) -> dict | None:
    """清单 404 时的兜底：直接问 GitHub API 最新 release。"""
    api = _api_latest_url(u)
    if not api:
        return None
    try:
        req = urllib.request.Request(
            api, headers={"User-Agent": mirrors.UA, "Accept": "application/vnd.github+json"})
        with _opener(proxy).open(req, timeout=8) as r:
            rel = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - 兜底失败就按原错误报
        diag.write(f"检查更新：清单 404，GitHub API 兜底也失败（{type(e).__name__}: {e}）", "update")
        return None
    info = _info_from_api_release(rel, current, api)
    if info:
        diag.write(f"检查更新：清单 404，改用 GitHub API 兜底：最新 v{info['version']}，"
                   f"有更新={info['has_update']}", "update")
    return info


def log_last_update_problem(current: str) -> str:
    """开机时调一次：有问题就写进日志（诊断报告会带走）"""
    msg = last_update_problem(current)
    if msg:
        try:
            # diag.write 自己会加 [tag] 前缀，这里别再拼一次（否则 [update] [update]）
            diag.write(" ".join(msg.split()), "update")
        except Exception:  # noqa: BLE001
            pass
    return msg


def check(current: str, url: str = "", proxy: str = "") -> dict:
    """检查是否有新版本。返回 {ok, has_update, version, url, notes, current, error}

    latest.json 里的 sha256 会被透传给下载步骤做完整性校验（老版本清单没有这个字段也没关系）。
    """
    last_kind = "未知错误"
    last_exc: BaseException | None = None
    saw_404 = False
    tried: list[str] = []
    configured = (proxy or "").strip()
    # 用户/系统已经给出代理就直接用，别再探一次：
    # 探测是 1.5 秒的快速请求，代理稍慢就会被判死，然后整个检查退成直连 → GitHub 超时
    # （2026-10-29 实测：日志里明明检测到 7897，检查更新却报 URLError）。
    proxy = configured or mirrors.effective_proxy({"proxy": ""})
    urls = _manifest_urls(url)
    info = None
    src = ""
    for _pass in range(2):
        for u in urls:
            tried.append(u)
            # GitHub 直连经常直接握手超时：给它很短的超时，尽快轮到 gh-proxy 镜像
            tmo = 4.0 if u.startswith("https://github.com/") else 12.0
            try:
                with _opener_for(u, proxy).open(u, timeout=tmo) as r:
                    info = json.loads(r.read().decode("utf-8"))
                src = u
            except Exception as e:  # noqa: BLE001 - 这个源不行就换下一个
                last_kind = type(e).__name__
                last_exc = e
                if isinstance(e, urllib.error.HTTPError) and int(getattr(e, "code", 0) or 0) == 404:
                    saw_404 = True
                info = None
                diag.write(f"检查更新：源 {u} 连不上（{type(e).__name__}: {e}）", "update")
                continue
            latest = str(info.get("version") or "").strip()
            pkg = str(info.get("url") or "").strip()
            if not latest or not pkg:
                last_kind = "清单格式不正确"
                info = None
                diag.write(f"检查更新：源 {u} 返回的清单缺版本号或下载地址", "update")
                continue
            has = _vtuple(latest) > _vtuple(current)
            diag.write(f"检查更新：当前 v{current}，最新 v{latest}，有更新={has}"
                       f"（源 {u}，共试了 {len(tried)} 个）", "update")
            return {"ok": True, "has_update": has, "version": latest, "current": current,
                    "pkg_url": pkg, "notes": str(info.get("notes") or ""),
                    "sha256": str(info.get("sha256") or "").strip().lower(),
                    "published": str(info.get("published") or ""), "source": src}

    # ★ 清单 404 = 服务器上确实没这个文件（发版时漏传），不是网络问题。
    #   这时候改问 GitHub API 要最新 release —— 装了新版却检测不到，问题比报错更难查。
    if saw_404:
        for u in dict.fromkeys(tried):
            if _api_latest_url(u):
                info = _api_fallback(u, proxy, current)
                if info:
                    return info
                break

    # ★ 每个源失败都写日志：以前只在成功时写，于是客户报「检测不到最新版」时，
    #   诊断报告里一条记录都没有 —— 分不清是「没新版」还是「压根没查成」。
    diag.write(f"检查更新失败（当前 v{current}）：试了 {len(tried)} 个源都不行，"
               f"最后一个是 {last_kind}", "update")
    return {"ok": False, "has_update": False,
            "error": f"检查更新失败（{last_kind}）——{check_failure_hint(last_exc or last_kind)}",
            "network": not isinstance(last_exc, urllib.error.HTTPError)}



#: 「网络不通 / 节点太慢」的异常痕迹（和「服务器 404」是两回事）
_NETWORK_MARKERS = (
    "timed out", "timeout", "handshake", "certificate verify failed",
    "urlopen error", "econnreset", "econnrefused", "connection reset",
    "connection aborted", "network is unreachable", "no route to host",
    "temporary failure in name resolution", "name or service not known",
    "10053", "10054", "10060", "10061",
    "远程主机强迫关闭", "目标计算机积极拒绝", "连接尝试失败", "连接超时",
    "太慢",
)


def download_failure_hint(last) -> str:
    """下载失败时后面那句人话（404 / 网络 / 其它分开说）

    ★ 2026-11-05 客户报障：发版后那几个小时 latest.json 已经上线、安装包还没传完，
      界面只显示「下载失败（已试过 3 条线路）：HTTPError: HTTP Error 404」，
      客户以为是自己的网络，反复点了十几分钟。404 和「节点太慢」必须分开说。
    """
    code = int(getattr(last, "code", 0) or 0)
    if code == 404:
        return ("——服务器上还没有这个安装包（刚发版、还没传完），不是你网络的问题；"
                "过一会儿再点「检查更新」。")
    text = str(last or "").lower()
    if any(m in text for m in _NETWORK_MARKERS):
        return "——网络不通或者代理节点太慢：请更换网络代理和节点后重试。"
    return ""


def check_failure_hint(kind) -> str:
    """检查更新全失败时的人话

    ★ 2026-11-07 客户报障：v6.60 release 漏传 latest.json，全程 404，
      界面却说「网络不通或者代理节点太慢」，客户反复换节点也没用。
      能收到 404 说明网络是通的 —— 服务器上没这个文件，必须和网络问题分开说。
    """
    if isinstance(kind, urllib.error.HTTPError):
        code = int(getattr(kind, "code", 0) or 0)
        if code == 404:
            return ("服务器上还没有更新清单（刚发版、还没传完），不是你网络的问题；"
                    "过一会儿再点「检查更新」，或者点「手动下载」。")
        if code in (403, 429):
            return "更新服务器暂时限流或拒绝访问，不是你网络的问题；过一会儿再试。"
        return f"更新服务器返回 HTTP {code}，过一会儿再试。"
    name = type(kind).__name__ if isinstance(kind, BaseException) else str(kind)
    if name in {"URLError", "HTTPError", "TimeoutError", "socket.timeout",
                "SSLError", "SSLCertVerificationError", "ConnectionError",
                "ConnectionResetError", "ConnectionAbortedError"}:
        return "网络不通或者代理节点太慢：请更换网络代理和节点后重试。"
    return "检查网络/代理后再试。"


#: 下载速度低于这个值（字节/秒）视为「这条线卡住了」，换下一条线路。
#: 2026-11-01 实测：同一条代理，新连接 10～12 MB/s，旧连接卡在 40 KB/s；
#: 原来的逻辑只在「报错」时重试，慢但不断线的连接会一直耗下去（207MB 要一小时）。
DOWNLOAD_SLOW_BPS = 200 * 1024
#: 判定窗口：先跑满这么久再测速，避免把握手阶段算进去
DOWNLOAD_SAMPLE_SEC = 15


def download(pkg_url: str, proxy: str, job, report, sha256: str = "") -> Path:
    """下载安装包到临时目录（支持取消、断点续传、慢速换线）

    下完必须校验：大小对不上（服务端给了 Content-Length）或 sha256 不符，就当场失败。
    半个安装包跑静默安装 = 更新装坏、程序再也起不来——这种事故比"下载失败"难查得多。

    ★ 2026-10-21 加重试：GitHub 会 302 到 release-assets.githubusercontent.com，
      那一段 TLS 偶发 EOF（实测过一次，同一时刻 curl 走同样的代理完全正常）。
    ★ 2026-11-01 加「慢速换线 + 续传」：代理挑到慢节点时连接是通的、只是 40 KB/s，
      旧逻辑永远等下去（客户实测 207MB 要一个多小时）。现在每 15 秒测一次速度，
      低于 200 KB/s 就换下一条线路，并用 Range 从已下载的字节继续，不白下。
    """
    dst = Path(tempfile.gettempdir()) / "StarryVT-Setup-new.exe"
    urls = _with_mirrors(pkg_url)
    attempts = len(urls)
    last: Exception | None = None
    # 设置里填的代理可能已经失效；这里和检查更新走同一套「填的能用就用、不能就自动探」
    proxy = (proxy or "").strip() or mirrors.effective_proxy({"proxy": ""})

    for attempt in range(1, attempts + 1):
        job.check()
        resume_from = dst.stat().st_size if dst.exists() else 0
        if attempt == 1:
            resume_from = 0
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            report("update", 0, "正在下载新版本…")
        elif resume_from > 0:
            report("update", None,
                   f"换一条线路续传（已下好 {resume_from // 1048576}MB）…")
        else:
            report("update", 0, f"下载中断，正在重试（{attempt}/{attempts}）…")

        u = urls[attempt - 1]
        try:
            req = urllib.request.Request(u)
            if resume_from:
                req.add_header("Range", f"bytes={resume_from}-")
            with _opener_for(u, proxy).open(req, timeout=45) as r:
                status = int(getattr(r, "status", 200) or 200)
                if resume_from and status == 206:
                    m = re.search(r"/(\d+)", r.headers.get("Content-Range") or "")
                    total = int(m.group(1)) if m else 0
                    got = resume_from
                    mode = "ab"
                else:
                    # 服务端不支持续传（或这是第一次）：从头来
                    resume_from = 0
                    got = 0
                    total = int(r.headers.get("Content-Length") or 0)
                    mode = "wb"
                with open(dst, mode) as f:
                    win_t = time.monotonic()
                    win_bytes = 0
                    while True:
                        job.check()
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        win_bytes += len(chunk)
                        now = time.monotonic()
                        if total:
                            speed = win_bytes / max(0.001, now - win_t)
                            report("update", int(got * 100 / total),
                                   f"下载中 {got // 1048576}MB / {total // 1048576}MB"
                                   f"（{speed / 1048576:.1f} MB/s）")
                        if now - win_t >= DOWNLOAD_SAMPLE_SEC:
                            if win_bytes / (now - win_t) < DOWNLOAD_SLOW_BPS:
                                raise RuntimeError(
                                    f"这条线路太慢（{win_bytes / (now - win_t) / 1024:.0f} KB/s），换线重试")
                            win_t, win_bytes = now, 0

            size = dst.stat().st_size
            if size < 1024 * 1024:
                raise RuntimeError("下载的安装包不完整（文件太小），请稍后重试")
            if total and size != total:
                raise RuntimeError(f"下载的安装包不完整（{size // 1048576}MB / 应为 {total // 1048576}MB），"
                                   f"请检查网络后重试")
            if sha256:
                report("update", None, "校验安装包…")
                if _sha256(dst) != sha256:
                    raise RuntimeError("安装包校验失败（下载过程中被改动或损坏），请重试")
            diag.write(f"新版本已下载: {dst}（{size // 1048576}MB）", "update")
            return dst

        except Cancelled:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception as e:  # noqa: BLE001 - 网络/校验错误都重试
            last = e
            diag.write(f"下载新版本失败（第 {attempt}/{attempts} 次）：{type(e).__name__}: {e}", "update")
            # 慢速换线时保留已下载的部分，下一次用 Range 续传；
            # 尺寸/校验错误说明文件本身不可信，直接删掉重来。
            if "不完整" in str(e) or "校验失败" in str(e) or "文件太小" in str(e):
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass
            if attempt < attempts:
                time.sleep(2 * attempt)

    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass
    raise RuntimeError(f"下载失败（已试过 {attempts} 条线路）：{type(last).__name__}: {last}"
                       + download_failure_hint(last))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_script(setup_path: Path, exe: str, pid: int, restart: bool,
                   also_name: str = "", target_version: str = "") -> str:
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
    # Inno 自己的安装日志：客户报「更新完版本没变」时，光看我们的时间线猜不出来
    # （2026-10-29 客户三次 errorlevel 5 时就没有）。日志目录同时会被诊断报告收走。
    instlog = "%APPDATA%\\StarryVT\\logs\\install-update.log"
    # 更新「到底换没换」的现场记录（.log 后缀：诊断报告只收 logs\*.log）
    marker = "%APPDATA%\\StarryVT\\logs\\update-last.log"
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        f'if not exist "%APPDATA%\\StarryVT\\logs" mkdir "%APPDATA%\\StarryVT\\logs"',
        f'{echo}update script started (pid={pid})>> "{logfile}"',
        f'set "APPEXE={exe}"',
        f'set "TARGET={target_version}"',
        'for %%I in ("%APPEXE%") do set "BEFORE=%%~zI|%%~tI"',
        f'{echo}install target: "%APPEXE%" (v%TARGET%), dir "{app_dir}">> "{logfile}"',        "rem Wait for the running app (our PID) to exit, up to ~60s, then force-kill.",
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
        f'/CLOSEAPPLICATIONS /DIR="{app_dir}" /LOG="{instlog}"',
        f'{echo}installer finished (errorlevel !ERRORLEVEL!)>> "{logfile}"',
        # ★ 2026-11-05 客户报障：更新完打开还是提示更新。安装程序可能返回 0 但
        #   文件没换掉（被占用/装到别处）。这里把「装之前/之后」的 exe 尺寸+时间
        #   记下来，下次诊断报告里一眼就能看出有没有真的替换。
        'for %%I in ("%APPEXE%") do set "AFTER=%%~zI|%%~tI"',
        f'{echo}exe before !BEFORE! -> after !AFTER!>> "{logfile}"',
        # 逐行写，别用 (echo a& echo b) 块：路径里带 `(` `)` `&` 时（Program Files (x86)
        # 之类）整个块会被 cmd 解析坏 —— 2026-11-05 写完就在这里踩过一次。
        f'echo target="!TARGET!"> "{marker}"',
        f'echo exe="!APPEXE!">> "{marker}"',
        f'echo dir="{app_dir}">> "{marker}"',
        f'echo before="!BEFORE!">> "{marker}"',
        f'echo after="!AFTER!">> "{marker}"',
        f'if "!BEFORE!"=="!AFTER!" {echo}WARNING 程序文件没变（更新可能没换成）>> "{logfile}"',
    ]
    if restart:
        lines.append(f'start "" "{exe}"')
        lines.append(f'{echo}app restarted>> "{logfile}"')
    lines.append("endlocal")
    return "\r\n".join(lines) + "\r\n"


def install_and_restart(setup_path: Path, restart: bool = True, target_version: str = ""):
    """脱离本进程执行：等本程序退出 → 静默安装 → 重新启动

    必须用独立脚本，不能拼一条 cmd 命令串（v1.0/v1.1 就是这么写的，实测出了两个坑）：
      1. `timeout /t 2` 在无控制台的进程里直接失败 → 等于没有等待；
      2. cmd 不等 GUI 程序（安装程序就是），`a.exe & b` 会立刻执行 b
         → 安装还没写完就把程序拉起来了，拉起来的是旧 exe，还顺手锁住了自己，
         安装程序替换失败 → 用户看到"更新完没自动打开 / 版本没变"。
    """
    exe = str(tools.exec_path())        # 长路径：短名会渗进更新脚本和日志
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
        _update_script(setup_path, exe, os.getpid(), restart, also_name, target_version),
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
