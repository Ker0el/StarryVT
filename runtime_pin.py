#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把引擎清单里的 ffmpeg 钉死到一个**日期构建**，不再每次安装都打 GitHub API。

## 为什么

finesub 的 `runtime-manifest.json` 里，ffmpeg 的 `asset` 是：

    {"url": ".../releases/download/latest/ffmpeg-n9.0-latest-win64-gpl-9.0.zip",
     "digest_from": "github-release-api"}

BtbN 的 `latest` 标签每天重建，所以引擎必须在安装时问 api.github.com
“今天这份文件的 sha256 是多少”。未认证 API 是 **60 次/小时、按出口 IP 计**。
国内共享出口很容易被打满；一旦 403，`finesub setup` 直接退出，运行环境就装不上
（2026-10-23 客户报障：反复重试越试越糟）。

这里选一个 **日期标签**（`autobuild-2026-10-23-13-04`，不可变）里的 n9.0 GPL 构建，
把 url / size / sha256 写死。引擎的 `resolve_asset` 看到 size + sha256 就不再调 API，
直接下文件 —— 这条路和 GitHub 的速率限制完全无关。

## 只钉“还没装 ffmpeg”的机器

已经装好 ffmpeg 的用户不动（否则 required_files 换了目录，引擎会把 194 MB 重下一遍）。
新装 / 资源缺失时才改写清单；装好之后 `current.json` 在，后续一律跳过。

## 上游资产万一被删

改写前先 HEAD 探一下（有加速镜像就探镜像地址）。探不通就不改，保留原来的
`digest_from` 路径 —— 慢，但至少还能用。
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import mirrors
import wheels

MANIFEST_NAME = "runtime-manifest.json"
BACKUP_SUFFIX = ".starryvt-pin-orig"      # 别和 engine_mirror 的 .starryvt-orig 抢

#: 选定的不可变构建（BtbN autobuild-2026-10-23-13-04）
PIN_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
           "autobuild-2026-10-23-13-04/"
           "ffmpeg-n9.0.2-35-g9a9b70bb8c-win64-gpl-9.0.zip")
PIN_SIZE = 193981220
PIN_SHA256 = "bc5c5d45a5bb24bb8b7e83eb6ca8ed9373b85098fbdde1e2951a48f91d91b54a"
PIN_TOP = "ffmpeg-n9.0.2-35-g9a9b70bb8c-win64-gpl-9.0"
PIN_REQUIRED = [f"{PIN_TOP}/bin/ffmpeg.exe", f"{PIN_TOP}/bin/ffprobe.exe"]


def manifests() -> list[Path]:
    """引擎里那张 runtime-manifest.json（和 pylock 在同一个目录）"""
    out: list[Path] = []
    for d in wheels._bootstrap_dirs():
        p = d / MANIFEST_NAME
        if p.is_file():
            out.append(p)
    return out


def _resource(doc: dict) -> dict | None:
    for res in doc.get("resources") or []:
        if isinstance(res, dict) and res.get("id") == "ffmpeg":
            return res
    return None


def _is_pinned(res: dict) -> bool:
    a = res.get("asset") or {}
    return (a.get("url") == PIN_URL
            and a.get("size") == PIN_SIZE
            and str(a.get("sha256") or "").lower() == PIN_SHA256
            and res.get("required_files") == PIN_REQUIRED)


def _home(params: dict) -> Path:
    raw = (str((params or {}).get("finesub_home") or "").strip()
           or os.environ.get("FINESUB_HOME", "").strip())
    if raw:
        return Path(raw)
    try:
        import datadir
        return datadir.default_home()
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FineSub"


def ffmpeg_installed(params: dict) -> bool:
    """引擎数据目录里已经有可用的 ffmpeg 资源了吗（包括旧版 latest 布局）"""
    root = _home(params) / "runtime" / "ffmpeg"
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8")).get("current")
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(current, str) or not current:
        return False
    version_dir = root / current
    return ((version_dir / "bin" / "ffmpeg.exe").is_file()
            or any(version_dir.glob("*/bin/ffmpeg.exe")))


def _probe_url(params: dict) -> str:
    """有加速镜像就探镜像上的地址（无代理用户也能验证）"""
    try:
        prefix = (mirrors.env(params or {}).get("FINESUB_GITHUB_FILE_PROXY") or "").strip()
    except Exception:  # noqa: BLE001
        prefix = ""
    return (prefix + PIN_URL) if prefix else PIN_URL


def _reachable(url: str, params: dict, timeout: float = 8.0) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "StarryVT"})
        with mirrors.opener(params or {}).open(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:  # noqa: BLE001 - 探不通就不改，走原来的 API 路径
        return False


def apply(params: dict | None = None, report=None) -> bool:
    """把尚未安装 ffmpeg 的引擎清单改写成本地钉死的资产。返回 True = 已钉/无需钉。"""
    params = params or {}
    files = manifests()
    if not files:
        return False                       # 引擎还没装

    # 已经装好 ffmpeg 的机器一律不动：换 required_files 会触发一次 194 MB 重下
    if ffmpeg_installed(params):
        return True

    # 全部已经是目标状态：不用探网，直接返回（每次开跑都会调到这里）
    docs: list[tuple[Path, dict, dict]] = []
    for f in files:
        try:
            doc = json.loads(wheels._read(f))
        except (OSError, ValueError):
            continue
        res = _resource(doc)
        if res is None or _is_pinned(res):
            continue
        docs.append((f, doc, res))
    if not docs:
        return True

    if not _reachable(_probe_url(params), params):
        if report:
            report("engine", None,
                   "钉死的 ffmpeg 构建当前探测不到，仍走 GitHub API（可能受速率限制）")
        return False

    for f, doc, res in docs:
        try:
            # 备份“改写前”的原件，便于诊断 / 回退
            backup = f.with_name(f.name + BACKUP_SUFFIX)
            wheels._write(backup, wheels._read(f))
            res["asset"] = {"url": PIN_URL, "size": PIN_SIZE, "sha256": PIN_SHA256}
            res["required_files"] = list(PIN_REQUIRED)
            wheels._write(f, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        except OSError as e:
            if report:
                report("engine", None, f"（ffmpeg 钉死写不进去：{e}）")
            continue
        if report:
            report("engine", None,
                   "已把 ffmpeg 钉死到日期构建（安装时不再需要 GitHub API）")
    return True


def restore() -> int:
    """还原成引擎原本的清单（诊断 / 排错用）"""
    n = 0
    for f in manifests():
        backup = f.with_name(f.name + BACKUP_SUFFIX)
        if backup.is_file():
            try:
                wheels._write(f, wheels._read(backup))
                backup.unlink(missing_ok=True)
                n += 1
            except OSError:
                pass
    return n


def status() -> dict:
    out = {"pin": {"url": PIN_URL, "size": PIN_SIZE, "sha256": PIN_SHA256}, "files": []}
    for f in manifests():
        try:
            doc = json.loads(wheels._read(f))
        except (OSError, ValueError):
            continue
        res = _resource(doc) or {}
        out["files"].append({
            "path": str(f),
            "pinned": _is_pinned(res),
            "asset": res.get("asset"),
            "backup": f.with_name(f.name + BACKUP_SUFFIX).is_file(),
        })
    return out
