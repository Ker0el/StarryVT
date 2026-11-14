#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把引擎运行环境（pylock）里的官方下载地址临时改写到国内镜像。

**为什么需要它**（2026-11-11 客户报告 诊断报告-20261111-224823）
  `finesub setup` 装约 6.7 GB 运行时，用的是引擎自带的
  `pylock.win-py312.toml`，里面每条依赖都钉死了**绝对 URL**
  （`files.pythonhosted.org` / `download-r2.pytorch.org`）。
  钉死 URL 的包不走 index，所以 `UV_DEFAULT_INDEX` / `--index-url` 那一类
  镜像设置对这一步**完全无效** —— 客户换了清华源照样卡住：
  cython 七次拿到同一个错误哈希，torch / llvmlite / silero-vad 传一半被掐
  （peer closed connection without sending TLS close_notify）。

**为什么能直接换 host**
  国内几家镜像（清华 / 阿里 / 北外 / 南大）的路径布局和官方一字不差、字节也一致
  （2026-11-14 实测：cython wheel 与 torch wheel 前 4 MB 的 sha256 与官方相同），
  所以只改协议+域名、哈希原样保留，uv 仍然会照常校验，校验不过就不会装。

**怎么用**
    import pypi_mirror
    pypi_mirror.restore()            # 上次被打断留下的改写，先还原
    if pypi_mirror.apply(report):    # 改文件（原文件备份成 *.starryvt-orig）
        try:
            ...跑 finesub setup...
        finally:
            pypi_mirror.restore()    # 不管成败都还原，别把引擎的文件留成改过的
"""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

#: 官方文件站（pylock 里钉的就是这些前缀）
PYPI_HOST = "https://files.pythonhosted.org/"
TORCH_HOSTS = ("https://download-r2.pytorch.org/whl/", "https://download.pytorch.org/whl/")

#: 候选镜像：(名字, PyPI 文件站前缀)。路径布局与 files.pythonhosted.org 完全一致。
PYPI_MIRRORS: tuple[tuple[str, str], ...] = (
    ("清华", "https://pypi.tuna.tsinghua.edu.cn/"),
    ("阿里", "https://mirrors.aliyun.com/pypi/"),
    ("北外", "https://mirrors.bfsu.edu.cn/pypi/web/"),
)

#: 候选 PyTorch 轮子镜像：(名字, 前缀)。注意阿里把 `whl` 那一层去掉了，
#: 所以这里是**整段前缀**的替换，不是换域名。
TORCH_MIRRORS: tuple[tuple[str, str], ...] = (
    ("阿里", "https://mirrors.aliyun.com/pytorch-wheels/"),
    ("南京大学", "https://mirror.nju.edu.cn/pytorch/whl/"),
)

BACKUP_SUFFIX = ".starryvt-orig"
PROBE_TIMEOUT = 5.0

#: 进程内缓存：探测一次就够（同一次安装里可能问好几遍）
_probe_cache: dict[str, bool] = {}
_last: dict = {"applied": False, "pypi": "", "torch": "", "locks": [], "error": ""}


# ---------------------------------------------------------------- 文件定位

def _tool_envs() -> list[Path]:
    """可能放着 uv 工具环境（finesub）的目录"""
    root = os.environ.get("UV_TOOL_DIR", "").strip()
    if root:
        roots = [Path(root)]
    else:
        appdata = os.environ.get("APPDATA", str(Path.home()))
        roots = [Path(appdata) / "uv" / "tools"]
    return [r / "finesub" for r in roots]


def locks() -> list[Path]:
    """引擎的 pylock 文件（`pylock.win-py312.toml` 之类）

    位置：`<工具环境>\\Lib\\site-packages\\finesub_cli\\_vendor\\src\\finesub_bootstrap\\`
    引擎自己还带一份 `pylock.win-py312.cn.toml`（地址全是清华源），按地区自动选；
    见 `canonical()` 与 `reset_degraded()`。
    """
    out: list[Path] = []
    for env in _tool_envs():
        base = (env / "Lib" / "site-packages" / "finesub_cli" / "_vendor" / "src"
                / "finesub_bootstrap")
        try:
            out.extend(sorted(base.glob("pylock.*.toml")))
        except OSError:
            continue
    return out


def canonical() -> Path | None:
    """官方源那份清单（`pylock.win-py312.toml`，不带 `.cn`）"""
    for p in locks():
        if ".cn." not in p.name:
            return p
    return None


# ---------------------------------------------------------------- 降级计数器

def _routes_file() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return None
    return Path(local) / "FineSub" / "download-routes.json"


def reset_degraded() -> bool:
    """把「这台机器放弃 pypi 镜像」的计数清掉，返回是否清过。

    ★ 2026-11-11 客户报告（诊断报告-20261111-224823）真正的死锁点：
      引擎自带 `pylock.win-py312.cn.toml`（全部指向清华源），只在
      「地区=cn 且 pypi 没有被判死」时才用。客户 11-10 用代理试了 3 次，
      代理把 TLS 流搞坏（`cannot decrypt peer's message`），第 3 次之后
      `download-routes.json` 里 `failures.pypi=3` → `is_degraded` 成立 →
      **引擎从此再也不用国内清单**，只剩官方源；而客户直连
      files.pythonhosted.org 正是传一半就断。失败 → 计数只增不减 → 永远好不了。
    """
    p = _routes_file()
    if p is None:
        return False
    try:
        import json
        body = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 没有这个文件/读不了：没什么可清的
        return False
    failures = body.get("failures")
    if not isinstance(failures, dict) or "pypi" not in failures:
        return False
    try:
        failures.pop("pypi", None)
        p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- 探测

def _head(url: str) -> bool:
    hit = _probe_cache.get(url)
    if hit is not None:
        return hit
    ok = False
    try:
        # 国内镜像不该走代理（客户机的代理经常正是拖住大文件的那个环节）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "StarryVT"})
        with opener.open(req, timeout=PROBE_TIMEOUT) as r:
            ok = 200 <= int(getattr(r, "status", 0) or 0) < 300
    except urllib.error.HTTPError as e:
        ok = 200 <= e.code < 300
    except Exception:  # noqa: BLE001 - 连不上就是不能用，不是错误
        ok = False
    _probe_cache[url] = ok
    return ok


def _first_url(text: str, prefix: str) -> str:
    """取文件里第一条以 prefix 开头的地址（探测用；没有就返回空串）"""
    i = text.find(prefix)
    if i < 0:
        return ""
    j = i
    while j < len(text) and text[j] not in '"\'>':
        j += 1
    return text[i:j]


def _pick_pypi(text: str) -> tuple[str, str]:
    """挑一个能用的 PyPI 镜像：(名字, 前缀)；都不行返回 ("", "")"""
    sample = _first_url(text, PYPI_HOST)
    if not sample:
        return "", ""
    for name, base in PYPI_MIRRORS:
        if _head(base + sample[len(PYPI_HOST):]):
            return name, base
    return "", ""


def _pick_torch(text: str) -> tuple[str, str]:
    """挑一个能用的 PyTorch 轮子镜像：(名字, 前缀)；都不行返回 ("", "")"""
    sample = ""
    host = ""
    for h in TORCH_HOSTS:
        sample = _first_url(text, h)
        if sample:
            host = h
            break
    if not sample:
        return "", ""
    rest = sample[len(host):]
    for name, base in TORCH_MIRRORS:
        if _head(base + rest):
            return name, base
    return "", ""


# ---------------------------------------------------------------- 改写 / 还原

def _rewrite(text: str, pypi_base: str, torch_base: str) -> str:
    if pypi_base:
        text = text.replace(PYPI_HOST, pypi_base)
    if torch_base:
        for h in TORCH_HOSTS:
            text = text.replace(h, torch_base)
    return text


def _backup(p: Path) -> Path:
    return p.with_name(p.name + BACKUP_SUFFIX)


def restore() -> int:
    """把改过的 pylock 还原（返回还原了几个）

    幂等：没有备份就什么都不做。每次安装结束后都该调一次，App 启动时也调一次
    （上一次要是被强杀，文件就留成改过的样子了）。
    """
    n = 0
    for p in locks():
        bak = _backup(p)
        if not bak.is_file():
            continue
        try:
            shutil.copyfile(bak, p)
            bak.unlink()
            n += 1
        except OSError:
            continue
    if n:
        _last.update(applied=False)
    return n


def _write(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def apply(report=None) -> bool:
    """把 pylock 的下载地址改到国内镜像（原文件留一份 `.starryvt-orig`）。

    返回是否真的改了。返回 False 时文件一定没动过（调用方照常走官方源）。
    """
    def say(msg: str) -> None:
        if report is not None:
            try:
                report("engine", None, msg)
            except Exception:  # noqa: BLE001 - 日志写不了不影响安装
                pass

    _last.update(applied=False, pypi="", torch="", locks=[], error="")
    files = locks()
    canon = canonical()
    if not files or canon is None:
        _last["error"] = "找不到引擎的 pylock"
        return False
    restore()                                  # 先还原上次没收拾干净的
    try:
        text = canon.read_text(encoding="utf-8")
    except OSError as e:
        _last["error"] = f"读不了 pylock：{e}"
        return False
    pypi_name, pypi_base = _pick_pypi(text)
    if not pypi_name:
        _last["error"] = "官方 pylock 里没有可换的地址（或国内 PyPI 镜像都连不上）"
        return False
    torch_name, torch_base = _pick_torch(text)
    if not torch_base:
        # torch 那条（2.6 GB，最大头）没镜像可用：整个镜像路线就不值当了，
        # 官方源都连不上的机器，只换小包也装不完。
        _last["error"] = "国内 PyTorch 镜像都连不上"
        return False
    changed = 0
    for p in files:
        try:
            orig = p.read_text(encoding="utf-8")
        except OSError:
            continue
        new = _rewrite(orig, pypi_base, torch_base)
        if new == orig:
            continue
        bak = _backup(p)
        if not bak.is_file():
            shutil.copyfile(p, bak)
        try:
            _write(p, new)
            changed += 1
        except OSError as e:
            _last["error"] = f"写不了 pylock：{e}"
            restore()
            return False
    if not changed:
        _last["error"] = "pylock 里没有可换的地址"
        return False
    _last.update(applied=True, pypi=pypi_name, torch=torch_name,
                 locks=[str(p) for p in files])
    say(f"改用国内下载镜像：PyPI {pypi_name} / PyTorch {torch_name}")
    return True


def describe() -> str:
    """给诊断报告用的一行说明"""
    if _last.get("applied"):
        return f"正在用国内镜像（PyPI {_last['pypi']} / PyTorch {_last['torch']}）"
    left = sum(1 for p in locks() if _backup(p).is_file())
    if left:
        return f"有 {left} 个 pylock 还是改过的状态（下次安装会先还原）"
    if _last.get("error"):
        return f"没启用（{_last['error']}）"
    return "没启用"
