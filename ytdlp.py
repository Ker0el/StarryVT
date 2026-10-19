#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""yt-dlp 的版本管理：装的是哪一版、要不要更新、更新到哪个通道

**为什么要单独一个模块**：yt-dlp 是随包分发的（`_internal\\bin\\yt-dlp.exe`），
以前只能靠"发一版新软件"顺带把它带上。可 YouTube 的反爬是按周变的、我们的发版
节奏是月——中间这几周用户撞上 403，看到的提示是"去官网下载最新版覆盖安装"，
而包里那份**其实已经是最新版**了（2026-10-07 查证：包里 2026.08.19 正是当时的
官方最新稳定版，照提示重装一遍什么问题都解决不了）。现在改成软件自己管这件事。

两个通道：

    stable   官方稳定版，约每月一更 —— 默认走这条
    nightly  每日构建 —— yt-dlp 官方对 YouTube 问题就是这么建议的

默认跟 stable，**只有真出事时才升 nightly**：pipeline 下载撞上反爬错误会调
`escalate()` 换通道再试一次。升上去之后不自动退回——nightly 的代码始终领先于
当时的稳定版，退回去只会让同一个问题再犯一次。

网络这块复用 mirrors.fetch：有代理直连官方，没代理就套国内加速镜像。
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import diag
import mirrors
import tools
from job import Cancelled, no_window_kwargs

STABLE = "stable"
NIGHTLY = "nightly"
CHANNELS = (STABLE, NIGHTLY)

_REPO = {STABLE: "yt-dlp/yt-dlp", NIGHTLY: "yt-dlp/yt-dlp-nightly-builds"}
UA = "StarryVT"

# 夜间构建的版本号形如 2026.09.27.232945，稳定版形如 2026.08.19
_RE_TAG = re.compile(r"/releases/tag/(\d[\d.]*)")
_RE_NUM = re.compile(r"\d+")

_local_ver: str | None = None      # 本进程读到的版本（更新成功后会被改写）


# ---------- 版本号 ----------

def _v(text: str) -> tuple:
    """版本号拆成数字元组，方便比大小：2026.09.27.232945 > 2026.08.19"""
    return tuple(int(x) for x in _RE_NUM.findall(str(text))) or (0,)


def is_newer(a: str, b: str) -> bool:
    """a 比 b 新吗（两边可以一个是稳定版格式、一个是 nightly 格式）"""
    return _v(a) > _v(b)


def _version_of(exe: Path) -> str:
    """跑一下这个 exe 读它的 --version；跑不起来（坏文件/被杀软拦）返回空串"""
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                           timeout=60, **no_window_kwargs())
    except Exception:  # noqa: BLE001 - 任何原因都算"读不到"
        return ""
    out = (r.stdout or "").strip()
    return out.splitlines()[0].strip() if out else ""


def local_version() -> str:
    """本机正在用的这一份的版本号（读不到返回空串）"""
    global _local_ver
    if _local_ver is None:
        p = tools.find("yt-dlp")
        _local_ver = _version_of(Path(p)) if p else ""
    return _local_ver


def current_channel(params: dict) -> str:
    """当前通道（没设过就是 stable）"""
    c = (params.get("ytdlp_channel") or "").strip().lower()
    return c if c in CHANNELS else STABLE


# ---------- 问线上要版本号 ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """要的就是那个 302 本身——它的 Location 里带着版本号"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _opener(params: dict):
    handlers: list = [_NoRedirect()]
    proxy = (params.get("proxy") or "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def remote_tag(channel: str, params: dict) -> str:
    """问 GitHub：这个通道当前的版本号是多少（拿不到返回空串）

    用 `github.com/<repo>/releases/latest` 的 302 Location 取版本号，**不查 API**：

      · `api.github.com` 不在加速镜像的覆盖范围里，没代理的机器根本连不上；
      · 它有 60 次/小时的匿名限流，用户多开几次就可能撞上。

    `/releases/latest` 会跳到 `/releases/tag/<版本>`，Location 里就是答案。
    """
    url = f"https://github.com/{_REPO[channel]}/releases/latest"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    where = ""
    try:
        with _opener(params).open(req, timeout=20) as r:
            where = r.headers.get("Location") or ""
    except urllib.error.HTTPError as e:
        # 302 被 urllib 当成错误抛出来——这正是我们要的那一次
        where = (e.headers or {}).get("Location") or ""
    except Exception:  # noqa: BLE001 - 连不上就当"问不到"，不影响已经装好的版本
        return ""
    m = _RE_TAG.search(where)
    return m.group(1) if m else ""


# ---------- 落盘 ----------

def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".starryvt-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _pick_target(exe: Path) -> Path:
    """决定新的 exe 放哪 —— **一律放用户目录**（`%APPDATA%\\StarryVT\\bin\\`）

    ★ 2026-10-16 改的（原来优先「就地替换」）：就地替换写在安装目录里，而**每次软件
      升级（装新版）都会把自更新下来的那份覆盖回随包的旧版** —— 实测日志：
      10-14 更到 nightly 2026.10.10 → 10-15 装完 3.7 又是 2026.08.19 → 又更一遍；
      今天装 3.8 后又变回 08.19。白下一次 17 MB，而且 settings 里那个「已装版本」
      也跟着失真。
      用户目录在 `tools` 的查找顺序里排在**最前**（用户级 → 随包 → 程序目录 → PATH），
      写那儿既立刻生效、又不会被升级覆盖。只有它真写不进去才退回就地替换。
    """
    d = tools.user_bin_dir()
    if _writable(d):
        return d / exe.name
    if _writable(exe.parent):          # 极端情况：退回以前的就地替换
        return exe
    return d / exe.name                # 都不行也不抛 —— 到写入那步会如实报错


def _remember(channel: str, version: str) -> None:
    try:
        import settings as cfg
        cfg.save({"ytdlp_channel": channel, "ytdlp_version": version})
    except Exception:  # noqa: BLE001 - 记不住顶多下次再问一遍，不影响这次已经换好的文件
        pass


def _say(report, line: str) -> None:
    diag.write(line, "ytdlp")
    if report:
        report("ytdlp", None, line)


# ---------- 对外接口 ----------

def _is_pe(p: Path) -> bool:
    """下回来的是不是一个 Windows 可执行文件（MZ 头）。

    ⚠️ 为什么要验：镜像限流时是**用 200 回一个 HTML 错误页**的，不验就会把那个网页
    写成 `yt-dlp.exe`，然后 `_version_of()` 去跑它 —— 报「下载回来的文件跑不起来」，
    把方向指向杀毒软件，其实是镜像根本没给文件。同一类问题见 mirrors.fetch 的注释。
    """
    try:
        return p.open("rb").read(2) == b"MZ"
    except OSError:
        return False


def state(params: dict) -> dict:
    """给界面和自检用：现在装的是哪一版、走的哪个通道、放在哪"""
    p = tools.find("yt-dlp") or ""
    return {"version": local_version() or "未知",
            "channel": current_channel(params),
            "path": p,
            "writable": _writable(Path(p).parent) if p else False}


def update(params: dict, report=None, channel: str = "", force: bool = False,
           job=None) -> dict:
    """检查并安装指定通道的最新版

    返回 {ok, changed, version, channel, error}。report 和流水线共用一套
    （stage, percent, line），界面上的日志区照样能看到；传 None 就是静默。

    传了 job 就是**可取消**的，而且取消点卡在"下载完了、还没替换"那一步：
    用户点取消，下回来的文件直接丢掉，**已经装好的那一份一个字节都不动**
    ——比起杀在半路换出一个残缺的 exe，宁可白下这 17MB。
    """
    ch = channel or current_channel(params)
    exe = tools.find("yt-dlp")
    if not exe:
        return {"ok": False, "error": "没找到随包的 yt-dlp.exe"}

    remote = remote_tag(ch, params)
    if not remote:
        return {"ok": False, "error": "连不上 GitHub（检查网络或代理设置）"}

    local = local_version()
    if not force and _v(remote) <= _v(local):
        _remember(ch, local)
        return {"ok": True, "changed": False, "version": local, "channel": ch}

    url = f"https://github.com/{_REPO[ch]}/releases/latest/download/yt-dlp.exe"
    dest = _pick_target(Path(exe))
    tmp = dest.with_name(dest.name + ".new")     # 放同一个目录：替换是原子的
    try:
        if job:
            job.check()
        _say(report, f"正在更新下载器：{ch} {remote}（{local or '未知'} → {remote}）")
        mirrors.fetch(url, tmp, params, report, stage="ytdlp", timeout=600,
                      validate=_is_pe)

        if job:
            job.check()                          # 取消了就到此为止，绝不往下走到替换
        got = _version_of(tmp)
        if not got:
            raise RuntimeError("下载回来的文件跑不起来（可能被杀毒软件拦了）")
        if not force and _v(got) <= _v(local):
            _say(report, f"下载器已是最新（{local}），不需要更新")
            return {"ok": True, "changed": False, "version": local, "channel": ch}
        # 同盘原子替换。被占用（有任务正在下载）时 Windows 会拒绝——那就下一轮再说，
        # 现场一个字节都不动，用户手上那份还是好好的。
        os.replace(tmp, dest)
    except Cancelled:
        tmp.unlink(missing_ok=True)
        _say(report, "已取消下载器更新（已经装好的那一份没有改动）")
        raise
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"替换 yt-dlp.exe 失败（被占用？）：{e}"}
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    global _local_ver
    _local_ver = got
    _remember(ch, got)
    _say(report, f"下载器已更新：{local or '未知'} → {got}（{ch}）")
    return {"ok": True, "changed": True, "version": got, "channel": ch}


def escalate(params: dict, report=None, job=None) -> bool:
    """下载被反爬挡住时自救：换到 nightly 再试一次

    返回 True = 已经换成 nightly 了，值得重跑一遍下载。
    已经在 nightly 上、或者换不过去（连不上 GitHub）就返回 False，
    调用方照旧用自己的重试节奏。
    """
    if current_channel(params) == NIGHTLY:
        return False
    _say(report, "下载被 YouTube 拦截，把下载器换成每日构建版再试一次…")
    r = update(params, report, channel=NIGHTLY, job=job)
    if not r.get("ok"):
        _say(report, f"（换成每日构建版没成功：{r.get('error') or '未知原因'}）")
        return False
    return bool(r.get("changed"))


def auto(params: dict, report=None) -> dict:
    """启动时的静默检查 + 更新：出任何问题都只写日志，不打扰用户"""
    try:
        return update(params, report)
    except Exception as e:  # noqa: BLE001 - 后台动作不该把界面搞崩
        diag.write(f"下载器自动更新跳过：{type(e).__name__}: {e}", "ytdlp")
        return {"ok": False, "error": str(e)}
