#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GitHub 加速镜像：让「没有代理的用户」也能下载必须出外网的那几样东西

首次运行要下的东西里，绝大多数本来就在国内镜像上（Python 包走清华 PyPI、
torch 走上海交大、语音模型走 hf-mirror），**只有 5 项落在 GitHub 上**：

    1. ctranslate2 wheel（16MB）   —— uv 按 pylock 里的 URL 直连，绕不开 → 见 wheels.py
    2. uv 本体（25MB）             —— 本模块自己下载，可以直接给 URL 加前缀
    3. 引擎用的 Python 3.12（21MB） —— uv 下载，认 UV_PYTHON_INSTALL_MIRROR
    4. 引擎自带 ffmpeg / MinGit    —— 引擎自己下载，认 FINESUB_GITHUB_FILE_PROXY
    5. 人声分离模型（0.64GB）      —— 同上

第 3、4、5 项**不用打包**：引擎和 uv 都留了官方的镜像入口（前者见
`finesub_bootstrap/download_sources.py` 的 "环境变量永远优先"，后者见
`uv python install --mirror`）。本模块负责挑一个**当下真的能通**的镜像，
再把这两个环境变量注进子进程。

镜像都是公益服务，谁都不敢保证一直活着，所以规则是：**用之前先测**，
一个不通就换下一个，全不通就退回官方源（有代理的用户照样能跑）。
用户也可以在设置里自己指定镜像，或者干脆关掉。
"""
from __future__ import annotations

import hashlib
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 候选镜像（前缀式：镜像地址 + 完整 GitHub 地址）。顺序按实测连通性排，
# 排在前面的是国内直连稳定、且有 HTTPS 的。用户可以自己加/改（设置 → GitHub 加速镜像）。
#
# ★ 这张表会**过期**：镜像站是公益性质的，关停/限流很常见。
#   2026-10-15 实测 22 个候选，只有 3 个活着（旧的 7 条里 6 条已死）；
#   2026-10-16 又掉一个 —— 而「没代理的用户能不能装上引擎」全押在这张表上，
#   所以**每次发版前重测一遍**。
#   测法（⚠️ 别用「能读回字节就算活」，那是错的：停放页也会回 200 + HTML）：
#     curl -sL --noproxy '*' -r 0-524287 -o x \
#       "<镜像>/https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip"
#   看 x 的头两个字节是不是 PK。-L 必须带（hk.gh-proxy.com 现在是 302 过去的）。
CANDIDATES: tuple[str, ...] = (
    "https://gh-proxy.org/",          # 2026-10-19 实测：真 ZIP（15MB uv.zip 正常），优先国内直连
    "https://hk.gh-proxy.com/",       # 2026-10-16 实测：302→hk.gh-proxy.org，跟跳转后回真 ZIP（0.31 MB/s）
    "https://gh-proxy.com/",          # 2026-10-16 实测：回真 ZIP（0.08 MB/s）
    # ⚠️ 2026-10-16 删掉 ghfile.geekertao.top：请求直接被重置（0 字节、181ms）。
    # ⚠️ ghproxy.cn 已删除（2026-10-15）：**域名到期变成了「This domain is for sale」
    #    停放页**，对 uv.zip 和我们的探测文件都回 200 + HTML。它就是「一键安装引擎 →
    #    BadZipFile」那个报障的真凶 —— 探针只看状态码，把停放页当成好镜像挑中，
    #    下回来的「uv.zip」其实是那张网页。**加新镜像时一定要真下一个二进制文件验一验**，
    #    只探根路径/小文件是验不出来的。
    # ⚠️ 2026-10-19 实测不通、别加回来：ghfast.top / github.moeyy.xyz / ghproxy.net /
    #    gh.llkk.cc / gh-proxy.net / gitproxy.click / ghproxy.cc / hub.gitmirror.com。
)

# 2026-10-17：用户决定只用 GitHub，不再维护腾讯云 COS 自建源（流量按 0.5 元/GB 计费）。
# 这张表留空；引擎清单里被老版本改到 COS 的地址由 engine_mirror 恢复成 GitHub 原始地址。
SELF_HOSTED: dict[str, str] = {}

# 探测用的小文件：本软件自己 Release 里的更新清单（1KB，稳定存在、永远可下）
PROBE_URL = "https://github.com/Ker0el/StarryVT/releases/latest/download/latest.json"

# uv 下载 Python 的镜像入口（uv 会在后面拼 /<tag>/<文件名>）
PYTHON_BUILD_STANDALONE = (
    "https://github.com/astral-sh/python-build-standalone/releases/download"
)

# 设置里的取值
AUTO = "auto"      # 自动挑选（默认）
OFF = "off"        # 不用镜像（直连；有代理的用户走这条）

UA = "StarryVT"
PROBE_TIMEOUT = 6.0
_total_budget = PROBE_TIMEOUT + 2.0
CACHE_TTL = 6 * 3600                 # 探测结果缓存 6 小时（一次探测要好几秒）

_cache: dict[str, str] = {}          # "auto" → 选中的前缀（空串 = 直连）


def _cached() -> str | None:
    """上次探测的结果（None = 没缓存；"" = 上次就没探到可用镜像）"""
    try:
        import settings as cfg
        c = cfg.load()
        if time.time() - float(c.get("github_mirror_at") or 0) < CACHE_TTL:
            m = str(c.get("github_mirror_ok") or "")
            # ★ 缓存里的地址**必须还在候选表里**才认。
            #   把挂掉的镜像从 CANDIDATES 删掉，删不掉用户设置里已经缓存的那一份 ——
            #   而 pick() 优先用缓存、不重新探测，于是那个死镜像还能被优先用满 6 小时。
            #   2026-10-15 客户实例：他设置里缓存着 ghproxy.cn（域名到期变停放页那个），
            #   升级到修好的版本之后照样先撞它一次。
            #   ⚠️ 只影响 auto：用户手填的地址不走 _cached()，那是他的选择，照样尊重。
            if m and m not in CANDIDATES:
                return None
            return m
    except Exception:  # noqa: BLE001 - 读不到就当没缓存
        return None
    return None


def _remember(prefix: str) -> None:
    try:
        import settings as cfg
        cfg.save({"github_mirror_ok": prefix, "github_mirror_at": time.time()})
    except Exception:  # noqa: BLE001 - 记不住顶多下次再探一遍
        pass


def _head(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """能不能**真的**从这个镜像拿到东西（取 1KB 的探测文件，顺便看一眼正文开头）

    ★ 光看状态码不够，两条实测的坑都要挡住：
      · 限流时用 200 回一个 HTML 错误页；
      · **域名到期变成「This domain is for sale」停放页**（`ghproxy.cn` 2026-10-15 就是这样）
        —— 它对我们那个探测文件也照回 200，旧探针把它当成好镜像挑中，
        真去下 `uv.zip` 拿到的是停放页，用户那边就是 `BadZipFile`。
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) >= 400:
                return False
            return not _is_html_bytes(r.read(512))
    except Exception:  # noqa: BLE001 - 探测失败就是不通，任何异常都算
        return False


# 代理可用性的探测缓存：{地址: (探测时刻, 通不通)}。5 分钟内不重复探，免得每次下载都白等 1.5 秒。
_proxy_cache: dict[str, tuple[float, bool]] = {}


def _probe_proxy(proxy: str) -> bool:
    """只做一次 TCP 连接测试（不建隧道、不校验证书），1.5 秒连不上就算不通。"""
    try:
        u = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except Exception:  # noqa: BLE001 - 连不上就是不通
        return False


def proxy_ok(params: dict) -> bool:
    """用户填的那个代理到底**通不通**（没填 = False）。

    ★ 为什么不只看「填没填」：默认值里曾经预填 http://127.0.0.1:7897，而没装代理软件的
      机器上那个端口没人监听，必然连不上（WinError 10061）。偏偏「填了代理」被当成
      「这人能上外网」的凭据，于是 GitHub 镜像和清华 PyPI 源全被跳过，请求全撞死端口，
      一键初始化 100% 失败，报错还只有一句 `uv.zip（10061）`，用户根本看不懂。
      （2026-10-15 客户实例。）
      现在改成真探一次：**探不通就当没填**，该走的镜像照走，用户不受影响。
    """
    p = (params.get("proxy") or "").strip()
    if not p:
        return False
    now = time.time()
    hit = _proxy_cache.get(p)
    if hit and now - hit[0] < 300:
        return hit[1]
    ok = _probe_proxy(p)
    _proxy_cache[p] = (now, ok)
    return ok


def enabled(params: dict) -> bool:
    """这台机器要不要用镜像：用户关掉了就直接不用；**且代理真的通**才不用"""
    mode = (params.get("github_mirror", AUTO) or AUTO).strip().lower()
    if mode in (OFF, "none", "-", "disable"):
        return False
    if proxy_ok(params):
        return False
    return True


def pick(params: dict, report=None, force: bool = False) -> str:
    """挑一个能用的镜像前缀；返回空串 = 用官方源（直连）

    用户自己填了地址就直接用（不测——他多半是从别处抄来的、自己试过的）；
    auto 才逐个探测，结果是**并发**测的，全不通最多等 8 秒。
    """
    if not enabled(params):
        return ""
    mode = (params.get("github_mirror", AUTO) or AUTO).strip()
    if mode.lower() != AUTO and mode:
        return mode if mode.endswith("/") else mode + "/"

    if not force and AUTO in _cache:
        return _cache[AUTO]
    if not force:
        remembered = _cached()
        if remembered is not None:       # 几小时内探过：直接用，别再等几秒
            _cache[AUTO] = remembered
            return remembered

    cands = list(CANDIDATES)
    got = ""
    try:
        with ThreadPoolExecutor(max_workers=len(cands)) as pool:
            futures = {pool.submit(_head, m + PROBE_URL): m for m in cands}
            for fut, mirror in futures.items():
                try:
                    if fut.result(timeout=_total_budget) and not got:
                        got = mirror
                except Exception:  # noqa: BLE001 - 超时/异常 = 这个镜像不能用
                    continue
    except Exception:  # noqa: BLE001 - 探测机制本身出错也要能继续
        got = ""

    _cache[AUTO] = got
    _remember(got)
    if report:
        if got:
            report("env", None, f"GitHub 加速镜像：{got}（无代理用户靠它下载外网依赖）")
        else:
            report("env", None, "⚠ GitHub 加速镜像都连不上，外网依赖将走官方源（无代理时可能失败）")
    return got


def env(params: dict, report=None) -> dict:
    """要注进子进程的环境变量（引擎用它们下载 ffmpeg/模型、uv 用它下载 Python）"""
    prefix = pick(params, report)
    if not prefix:
        return {}
    return {
        "FINESUB_GITHUB_FILE_PROXY": prefix,
        "UV_PYTHON_INSTALL_MIRROR": prefix + PYTHON_BUILD_STANDALONE,
    }


def prefixed(url: str, params: dict) -> str:
    """给一个 GitHub 地址套上镜像前缀（挑不到镜像就原样返回）"""
    if not url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")):
        return url
    prefix = pick(params)
    return (prefix + url) if prefix else url


def _is_html_bytes(b: bytes) -> bool:
    """这坨字节像不像一个网页。

    ⚠️ **不能只看 `<!doctype` / `<html` 开头** —— 实测 `ghproxy.cn` 域名到期变成
      「This domain is for sale」停放页之后，正文是
      `<!-- 964673484888 -->\\n<!DOCTYPE html>\\n<html …`，
      **前面挂着一句 HTML 注释**，只匹配开头会漏掉它（2026-10-15 客户报障的真凶）。

    判据：剥掉空白和开头的注释之后**以 `<` 开头**。我们要下的是 zip(PK) / exe(MZ) /
    whl(PK) / 清单({)，没有任何一种是 `<` 开头的，所以这条不会误伤。
    """
    b = b.lstrip()
    while b[:4] == b"<!--":
        end = b.find(b"-->")
        if end < 0:
            return True                 # 注释都没闭合，肯定不是正经文件
        b = b[end + 3:].lstrip()
    return b.startswith(b"<")


def _looks_like_html(p: Path, content_type: str = "") -> bool:
    """下到的到底是不是一个网页。

    ★ 这是镜像最坑的一种失败：**限流 / 域名到期 / 上游挂了的时候用 HTTP 200 返回一个
      HTML 页面**，状态码完全正常，只有正文能看出来。不认出来的话，后面 `zipfile` 就是
      一句 `BadZipFile: File is not a zip file` —— 用户看到的只有这一句，谁也定位不到
      「其实镜像给我的是网页」。（2026-10-15 客户报障。）
    """
    if "html" in content_type:
        return True
    try:
        return _is_html_bytes(p.open("rb").read(512))
    except OSError:
        return False


def _candidate_urls(url: str, params: dict) -> list[str]:
    """要依次试的地址：挑中的镜像 → 再垫两个备选镜像 → 最后官方直连

    ★ 只试「挑中的那一个」不够：`pick()` 的结果会缓存几个小时（探测一次要好几秒），
      而镜像在这期间变坏是很常见的 —— 限流之后改成回 HTML 错误页，用户就卡死了。
      多垫两个的成本只是失败时多几秒。
    """
    urls: list[str] = []
    # 自建的那一份永远排第一：它比公益镜像快一个数量级，而且是我们自己的。
    # 顺带说明**为什么它不受 `github_mirror` 设置影响** —— 那个设置管的是「要不要用
    # 第三方的 GitHub 加速站」，我们自己的存储不属于那一类。它要是下不动，
    # 下面照旧会按用户的设置往下试，所以没有「把用户的选择顶掉」这回事。
    ours = SELF_HOSTED.get(url)
    if ours:
        urls.append(ours)
    if url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")) \
            and enabled(params):
        mode = (params.get("github_mirror", AUTO) or AUTO).strip()
        if mode.lower() != AUTO and mode:          # 用户手填的地址：只认他这一个，别自作主张换
            chosen = mode if mode.endswith("/") else mode + "/"
            urls.append(chosen + url)
        else:
            chosen = pick(params)
            cands = ([chosen] if chosen else []) + [m for m in CANDIDATES if m != chosen][:2]
            urls += [m + url for m in cands]
    if url not in urls:
        urls.append(url)                           # 兜底永远是官方直连
    return urls


def fetch(url: str, dest: Path, params: dict, report=None, stage: str = "env",
          timeout: float = 120.0, sha256: str = "", validate=None) -> Path:
    """下载一个 GitHub 上的文件：先试镜像，再试直连；返回落地路径

    sha256 非空时校验（镜像和直连下到的字节必须一致，可以放心用公益镜像）。
    validate 是一个「这个文件是不是我想要的」的判据（如 `zipfile.is_zipfile`），
    **每个来源下完都跑**：不通过就当这个来源失败、换下一个。

    ⚠️ 为什么必须有校验：**状态码 200 不代表下到的是那个文件**。镜像限流、上游 404、
    反爬拦截都可能给你一个 HTML 页面，字节数看着还挺像回事。2026-10-15 客户
    「一键安装引擎」炸在 `BadZipFile: File is not a zip file`，根因就是这个 ——
    当时这个函数一个校验都没做，下到什么都往磁盘上写、直接交给调用方去解压。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = _candidate_urls(url, params)

    # 用户配了代理**而且它真的通**才带上（这台机器自己的进程环境里通常没有 HTTP(S)_PROXY）。
    # ⚠️ 必须过 proxy_ok：端口上没人监听的时候硬走代理，所有来源会一起失败。
    opener = urllib.request.build_opener()
    proxy = (params.get("proxy") or "").strip()
    if proxy_ok(params):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    last = ""
    for i, u in enumerate(urls):
        try:
            if report:
                # 日志里必须能一眼看出「这份是从哪下来的」—— 客户报「下载特别慢」时，
                # 第一件事就是问他日志里写的是哪个源。
                if u == SELF_HOSTED.get(url):
                    tag = "（自建源）"
                elif u != url:
                    tag = f"（加速镜像 {i + 1}）"
                else:
                    tag = "（官方源）"
                report(stage, None, f"下载 {dest.name} …{tag}")
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            h = hashlib.sha256() if sha256 else None
            ctype = ""
            with opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
                ctype = (r.headers.get("Content-Type") or "").lower()
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    if h:
                        h.update(chunk)
                    got += len(chunk)
                    if report and total:
                        report(stage, None,
                               f"下载 {dest.name} {got // 1024 // 1024}MB / {total // 1024 // 1024}MB")

            # ---- 下完一律先验，四条里中一条就当这个来源失败、换下一个 ----
            if got == 0:
                last = f"{dest.name} 是 0 字节"
                dest.unlink(missing_ok=True)
                continue
            if total and got != total:
                last = (f"{dest.name} 只下到 {got // 1024}KB / {total // 1024}KB"
                        f"（连接被中断，多半是镜像不稳）")
                dest.unlink(missing_ok=True)
                continue
            # 有 sha256 / validate 时不必再看内容类型：它们比「像不像网页」准得多
            if not sha256 and validate is None and _looks_like_html(dest, ctype):
                last = f"{dest.name} 下到的是网页而不是文件（镜像返回了错误页）"
                dest.unlink(missing_ok=True)
                continue
            if sha256 and h and h.hexdigest().lower() != sha256.lower():
                last = f"{dest.name} 校验失败（下到的文件不对）"
                dest.unlink(missing_ok=True)
                continue
            if validate is not None and not validate(dest):
                last = f"{dest.name} 下到的文件不是预期的格式（可能被镜像换成了错误页）"
                dest.unlink(missing_ok=True)
                continue
            return dest
        except Exception as e:  # noqa: BLE001 - 这个来源不行就换下一个
            last = str(e)
            dest.unlink(missing_ok=True)
    # 代理填了但连不上，是这里最常见的失败原因，而且报错只有一串 WinError 谁也看不懂。
    # 直接把话说明白，用户照着做就能自救。
    if proxy and not proxy_ok(params):
        raise RuntimeError(
            f"下载失败：{dest.name}\n"
            f"连不上你设置的代理 {proxy}。如果本机没开代理软件，"
            f"去「设置 → 网络代理」把这一栏清空再试；\n"
            f"清空后会自动改用国内的加速镜像下载。")
    raise RuntimeError(f"下载失败：{dest.name}（{last}）")


def describe(params: dict) -> list[str]:
    """给诊断报告用：当前镜像状态"""
    mode = (params.get("github_mirror", AUTO) or AUTO).strip()
    if not enabled(params):
        why = "用户关闭" if mode.lower() in (OFF, "none", "-", "disable") else "已配代理（直连 GitHub）"
        return [f"GitHub 加速镜像：不用（{why}）"]
    chosen = _cache.get(AUTO)
    if chosen is None:
        chosen = _cached()               # 本进程还没探过，看看上次记下来的
    if chosen:
        return [f"GitHub 加速镜像：{chosen}"]
    if chosen is not None:
        return ["GitHub 加速镜像：候选都不通，走官方源"]
    return [f"GitHub 加速镜像：{mode}（开工时自动探测）"]
