#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GitHub 上那一个绕不开的包（ctranslate2）：随包分发 + 本地小服务喂给 uv

引擎装运行时依赖时，uv 会按 pylock 里写死的 URL 直连 GitHub 下载 ctranslate2
这个定制 wheel。国内没代理的机器上这一步**必然失败**（实测
`tcp connect error / deadline has elapsed`），而 uv 明确不允许绕开：

    UV_OVERRIDE = <本地文件>   → error: Cannot specify overrides with a pylock.toml file
    pylock 里写 file://…      → error: URL scheme is not allowed

（两条都在 uv 0.12.13 上实测过。）唯一可行的办法是**改 pylock 里那一条 URL**，
改成本机回环的明文 HTTP（uv 允许 http://127.0.0.1:port/…，同样实测通过）：

    wheel 随安装包分发（16MB）→ 起一个只监听回环的静态服务 → 把 pylock 指过去

于是无代理、甚至断网的机器也能把运行时装完；上游换版本（hash 对不上）时，
再退回 mirrors.py 的加速镜像去下。改之前会留一份 `.starryvt-orig` 备份。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import mirrors
import tools

# 只监听回环，不对外开端口（不会弹 Windows 防火墙）
HOST = "127.0.0.1"
PORT = 51873

LOCALHOST = "http://127.0.0.1:"
BACKUP_SUFFIX = ".starryvt-orig"

# pylock 里一条依赖长这样（url 和 sha256 在同一行）：
#   archive = { url = "https://github.com/…/ctranslate2-….whl", hashes = { sha256 = "…" } }
_ENTRY = re.compile(r'url\s*=\s*"(https://github\.com/[^"\s]+)"[^\n]*?sha256\s*=\s*"([0-9a-fA-F]{64})"')
_LOOPBACK = re.compile(r"http://127\.0\.0\.1:\d+/")

_server: ThreadingHTTPServer | None = None
_port = 0
_lock = threading.Lock()
_verified: dict[str, str] = {}          # 文件名 → 已校验过的 sha256（免得每次重算 16MB）


# ---------- 位置 ----------

def cache_dir() -> Path:
    """wheel 的落地目录（持久，重装软件也不用重下；跟着「数据目录」走）"""
    import paths
    return paths.resolve("wheels")


def bundle_dir() -> Path:
    """随包分发的 wheel 目录（打包后在 _internal\\wheelhouse）

    目录名特意不叫 wheels —— 那会和本模块 wheels.py 撞名，
    同目录下存在 wheels\\ 时 `import wheels` 会先找到目录。
    """
    return tools.bundle_dir() / "wheelhouse"


def _bootstrap_dirs() -> list[Path]:
    """引擎（finesub）里放 pylock 的目录：uv 的 tool 安装目录下"""
    tail = Path("Lib") / "site-packages" / "finesub_cli" / "_vendor" / "src" / "finesub_bootstrap"
    roots: list[Path] = []
    tool_dir = os.environ.get("UV_TOOL_DIR", "").strip()
    if tool_dir:
        roots.append(Path(tool_dir))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        roots.append(Path(appdata) / "uv" / "tools")
    out: list[Path] = []
    for root in roots:
        cand = root / "finesub" / tail
        if cand.is_dir():
            out.append(cand)
        try:                                   # uv 的目录被改过时兜底找一遍
            out.extend(p for p in root.glob(f"*/{tail.as_posix()}") if p.is_dir())
        except OSError:
            continue
    return list(dict.fromkeys(out))


def pylocks() -> list[Path]:
    """所有带 GitHub 依赖的 pylock（cn / 官方两份，内容一样的那一条也在里面）"""
    out: list[Path] = []
    for d in _bootstrap_dirs():
        try:
            out.extend(sorted(d.glob("pylock.*.toml")))
        except OSError:
            continue
    return out


# ---------- 文件 ----------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ok(path: Path, sha: str) -> bool:
    """文件在、且是我们要的那一份（结果按 路径+大小+时间 记住，不重复算）"""
    try:
        key = f"{path}|{path.stat().st_size}|{path.stat().st_mtime_ns}"
    except OSError:
        return False
    if _verified.get(key) == sha:
        return True
    try:
        if _sha256(path).lower() != sha.lower():
            return False
    except OSError:
        return False
    _verified[key] = sha
    return True


def _name_of(url: str) -> str:
    """URL 里的文件名（uv 要求它是个合法的 wheel 名，所以必须原样保留）"""
    return unquote(url.rsplit("/", 1)[-1])


def _local(url: str, sha: str, params: dict, report=None) -> Path | None:
    """把这一条依赖弄到本地：缓存 > 随包分发 > 加速镜像下载"""
    name = _name_of(url)
    cached = cache_dir() / name
    if _ok(cached, sha):
        return cached

    bundled = bundle_dir() / name
    if _ok(bundled, sha):
        try:
            cache_dir().mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundled, cached)
            return cached
        except OSError:
            return bundled            # 复制不了就直接用随包那一份

    try:
        return mirrors.fetch(url, cached, params, report, stage="engine", sha256=sha)
    except Exception as e:  # noqa: BLE001 - 下不动就如实说，让调用方保留原样
        if report:
            report("engine", None, f"⚠ {name} 没能准备好（{e}）——稍后由引擎自行下载")
        return None


# ---------- 本地小服务 ----------

class _Quiet(SimpleHTTPRequestHandler):
    """只发文件，不写日志（uv 会连很多次，日志会淹掉界面）"""

    def log_message(self, *args):  # noqa: D102 - 覆盖父类即可
        pass


def serve() -> int:
    """起（或复用）本地服务，返回端口；起不来返回 0"""
    global _server, _port
    with _lock:
        if _port:
            return _port
        try:
            cache_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            return 0
        handler = partial(_Quiet, directory=str(cache_dir()))
        for port in (PORT, 0):               # 先试固定端口（地址稳定），被占就让系统分配
            try:
                srv = ThreadingHTTPServer((HOST, port), handler)
            except OSError:
                continue
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True,
                             name="starryvt-wheels").start()
            _server, _port = srv, srv.server_address[1]
            return _port
        return 0


# ---------- 改 pylock ----------

def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "surrogateescape")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def _original_text(path: Path, text: str = "") -> str:
    """这个 pylock **改写前**的内容

    已经改过的（含回环地址）从备份里取原件——端口每次可能不一样，
    所以每次开工都要拿原始内容重新改一遍，不能只看“有没有 GitHub 地址”。
    """
    if not text:
        text = _read(path)
    if LOCALHOST not in text:
        return text
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.is_file():
        orig = _read(backup)
        if orig and LOCALHOST not in orig:
            return orig
    return _LOOPBACK.sub("", text)           # 没有可用备份：把回环前缀剥掉


def entries(path: Path) -> list[tuple[str, str]]:
    """这个 pylock 里的 GitHub 依赖：(url, sha256)（已改写过的返回改写前的那条）"""
    try:
        return _ENTRY.findall(_original_text(path))
    except OSError:
        return []


def _patch(path: Path, mapping: dict[str, str], port: int) -> bool:
    """把 GitHub 地址换成本机回环地址；改之前留备份，重复运行不会叠加"""
    text = _read(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    orig = _original_text(path, text)

    if LOCALHOST not in text and (not backup.is_file() or LOCALHOST in _read(backup)):
        try:
            _write(backup, orig)             # 首次改写：留一份原始内容
        except OSError:
            pass

    new = orig
    for url, name in mapping.items():
        new = new.replace(url, f"{LOCALHOST}{port}/{name}")
    if new == text:
        return False
    try:
        _write(path, new)
    except OSError:
        return False
    return True


def restore() -> int:
    """把所有 pylock 还原成引擎原本的样子（诊断/排错用）"""
    n = 0
    for f in pylocks():
        backup = f.with_name(f.name + BACKUP_SUFFIX)
        if backup.is_file():
            try:
                _write(f, _read(backup))
                n += 1
            except OSError:
                continue
        if LOCALHOST in _read(f):            # 没有备份的：手动剥掉回环前缀
            _write(f, _LOOPBACK.sub("", _read(f)))
    return n


# ---------- 对外的入口 ----------

def prepare(params: dict, report=None) -> bool:
    """开工前调一次：让引擎装运行时时不再需要访问 GitHub

    返回 True = 该准备的都准备好了（或者本来就不需要准备）。
    """
    files = pylocks()
    if not files:
        return False                          # 引擎还没装，等装完再说

    need: dict[str, str] = {}
    for f in files:
        for url, sha in entries(f):
            need[url] = sha
    if not need:
        return True                           # 上游已经不用 GitHub 了，无事可做

    mapping: dict[str, str] = {}
    for url, sha in need.items():
        path = _local(url, sha, params, report)
        if not path:
            return False
        mapping[url] = _name_of(url)

    port = serve()
    if not port:
        return False
    changed = [f for f in files if _patch(f, mapping, port)]
    if changed and report:
        report("engine", None, f"已把 {len(mapping)} 个 GitHub 依赖改为本机分发（共 {len(changed)} 个文件）")
    elif report:
        report("engine", None, "GitHub 依赖已就绪（本机分发）")
    return True


def describe() -> list[str]:
    """给诊断报告用：这些 GitHub 依赖现在是什么状态"""
    out: list[str] = []
    files = pylocks()
    if not files:
        return ["GitHub 依赖：引擎未安装"]
    for f in files:
        try:
            text = _read(f)
        except OSError:
            continue
        local = re.findall(r"http://127\.0\.0\.1:\d+/([^\"\s]+)", text)
        if not local and not entries(f):
            out.append(f"{f.name}：无 GitHub 依赖")
            continue
        if local:                                # 已改成本机分发
            for name in local:
                name = unquote(name)
                have, where = cache_dir() / name, "本机分发"
                if not have.is_file() and (bundle_dir() / name).is_file():
                    have, where = bundle_dir() / name, "随包分发"
                size = f"（{have.stat().st_size // 1024 // 1024}MB，已就绪）" if have.is_file() else "（❌ 文件缺失）"
                out.append(f"{name}：{where}{size}")
            continue
        for url, _sha in entries(f):             # 还没改写
            name = _name_of(url)
            if (bundle_dir() / name).is_file():
                out.append(f"{name}：随包分发（等改写 pylock）")
            elif (cache_dir() / name).is_file():
                out.append(f"{name}：已缓存（等改写 pylock）")
            else:
                out.append(f"{name}：❌ 还没下载")
    if _port:
        out.append(f"本机分发服务：{LOCALHOST}{_port}/")
    return out
