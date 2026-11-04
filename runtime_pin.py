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
import shutil
import urllib.request
import zipfile
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

    # ★ 2026-11-05：探测**不再决定**钉不钉。
    #   以前这里探不通就 `return False`，退回 GitHub API 路径 —— 而「探不通」的正是
    #   没代理 / 节点不稳的那些用户，于是他们每次安装都要打 api.github.com
    #   （未认证 60 次/小时、按出口 IP 计），撞上 403 就永远装不上。
    #   钉死的地址是不可变 tag，探不到多半只是本地线路抖动；真下不动时
    #   `_provision_runtime` 会 restore() 回退到引擎原来的路径并重试一次。
    if not _reachable(_probe_url(params), params) and report:
        report("engine", None,
               "（钉死的 ffmpeg 构建这次探测不到，仍按钉死地址安装；下不动会自动回退）")

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


# ===================================================== 随包预置（不走引擎下载）
#
# 2026-11-05 一批新用户集中报障：「引擎本体已装好，运行环境还没准备好」。
#
# 根因：`finesub setup` 里**只有 ffmpeg 是必装资源**（shell.ensure_ready 只调
# `_ensure_resource("ffmpeg")`，git / tokcount 都是 optional）。而 ffmpeg 这一项：
#   · 没有钉死时，sha256 要问 api.github.com（未认证 60 次/小时、按出口 IP 计）；
#   · 有钉死时，文件本身仍要经 `FINESUB_GITHUB_FILE_PROXY` 给的**单个**镜像前缀下载。
# 国内没代理的新用户，镜像一慢一死这一步就必然失败 —— 而界面上只看得到一句
# 「运行环境还没准备好」。
#
# 这里改成「先把文件放到磁盘上」：引擎的 `ResourceManager.install()` 一看到
# `required_files` 齐全，就直接写 current.json 返回 ready，**连网络都不碰**。
#
# 来源优先级：
#   1. 随包自带的 ffmpeg（`.tools\bin`，与钉死的那份是同一个 commit 的构建，
#      LGPL 共享库版：ffmpeg.exe + av*.dll + sw*.dll）
#   2. 随包的 `bundle_runtime\<资源 id>.zip`（MinGit / tokcount 这类小件）
#   3. `mirrors.fetch` —— app 自己的多镜像回退 + sha256 校验（比引擎那套稳）
#
# 全部失败也不算错：引擎会按它原来的路径自己下。
BUNDLE_DIR = "bundle_runtime"

#: 需要预置的资源（顺序即尝试顺序）。git 在引擎里标注 optional: no longer used，
#: tokcount 只在 LLM 阶段用，两者都不是 setup 的硬依赖，但装上能省掉运行时的下载。
PREFETCH_IDS = ("ffmpeg", "tokcount", "git")


def _specs() -> dict[str, dict]:
    """从引擎清单里读每个资源：version / required_files / asset"""
    out: dict[str, dict] = {}
    for f in manifests():
        try:
            doc = json.loads(wheels._read(f))
        except (OSError, ValueError):
            continue
        for res in doc.get("resources") or []:
            if not isinstance(res, dict):
                continue
            rid = str(res.get("id") or "")
            if rid and res.get("version") and res.get("required_files"):
                out.setdefault(rid, res)
    return out


def _resource_root(params: dict, spec: dict) -> Path:
    base = _home(params) / "runtime"
    return base / str(spec.get("directory") or spec["id"])


def _files_present(base: Path, required: list[str]) -> bool:
    return base.is_dir() and all((base / Path(r)).is_file() for r in required)


def ffmpeg_ready(params: dict) -> bool:
    """引擎的 ffmpeg 资源已经齐了吗（文件齐全 + current.json 指向它）

    和引擎 `ResourceManager.status()` 的判据保持一致：只有 current.json 指着的那份
    才算就绪，光有文件目录没用。
    """
    spec = _specs().get("ffmpeg")
    if not spec:
        return False
    root = _resource_root(params, spec)
    version = str(spec["version"])
    if not _files_present(root / version, [str(x) for x in spec["required_files"]]):
        return False
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8")).get("current")
    except (OSError, ValueError, AttributeError):
        return False
    return current == version


def _app_bin() -> Path | None:
    """随包的 .tools\\bin（ffmpeg / ffprobe / av*.dll / sw*.dll）"""
    try:
        import tools
    except Exception:  # noqa: BLE001
        return None
    for cand in (tools.bundle_dir() / "bin", tools.app_dir() / "bin",
                 tools.app_dir() / ".tools" / "bin"):
        if (cand / "ffmpeg.exe").is_file() and (cand / "ffprobe.exe").is_file():
            return cand
    return None


def _bundle_zip(rid: str) -> Path | None:
    try:
        import tools
        base = tools.bundle_dir() / BUNDLE_DIR
    except Exception:  # noqa: BLE001
        return None
    for name in (f"{rid}.zip", f"{rid}-latest.zip"):
        p = base / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _safe_extract(archive: Path, dest: Path) -> None:
    """解压到 dest（只接受相对路径、不越界）。包是我们自己钉死 sha256 的，仍按规矩来。"""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(root)):
                raise ValueError(f"压缩包里有越界路径：{info.filename}")
        z.extractall(dest)


def _hoist_single_dir(staging: Path, required: list[str]) -> None:
    """压缩包多套一层目录时，把它抬成正根（MinGit 是平的，BtbN 的 ffmpeg 套一层）"""
    if _files_present(staging, required):
        return
    children = [p for p in staging.iterdir() if p.is_dir()]
    if len(children) != 1:
        return
    inner = children[0]
    if not _files_present(inner, required) and not any(
            (inner / Path(r)).exists() for r in required):
        return
    for item in list(inner.iterdir()):
        os.replace(item, staging / item.name)
    inner.rmdir()


def _activate(staging: Path, root: Path, version: str, required: list[str]) -> bool:
    """把 staging 换成 root\\<version> 并写 current.json（引擎据此认为资源就绪）"""
    if not _files_present(staging, required):
        return False
    final = root / version
    root.mkdir(parents=True, exist_ok=True)
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    try:
        os.replace(staging, final)
    except OSError:
        shutil.move(str(staging), str(final))
    if not _files_present(final, required):
        return False
    tmp = root / "current.json.tmp"
    tmp.write_text(json.dumps({"current": version}), encoding="utf-8")
    os.replace(tmp, root / "current.json")
    return True


def _prefetch_one(rid: str, params: dict, spec: dict, report) -> bool:
    required = [str(x) for x in spec["required_files"]]
    version = str(spec["version"])
    root = _resource_root(params, spec)

    if _files_present(root / version, required):
        # 文件在，但 current.json 可能还没写（上次半途而废）
        if (root / "current.json").is_file():
            return True
        return _activate_same_dir(root, version, required)

    root.mkdir(parents=True, exist_ok=True)
    staging = root / f"{version}.starryvt-staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    asset = spec.get("asset") or {}
    want_sha = str(asset.get("sha256") or "").lower()

    # 1) 随包的 ffmpeg —— 和钉死的那份同源构建，直接抄目录（含 DLL）
    #    ⚠️ 随包这份是 **LGPL** 构建（没有 libx264/libx265）。引擎只在
    #    `media/ffmpeg.py` 的「多模态视频片段」里用 libx264，而那条路要
    #    `correction_media == "video"` 才会走；我们生成的模型目录里
    #    DeepSeek 的 supports_video=false，所以实际跑不到。哪天接了多模态模型，
    #    要么换回引擎自己下载的 GPL 构建，要么给引擎单独放一份。
    if rid == "ffmpeg":
        bin_dir = _app_bin()
        parts = Path(required[0]).parts
        top = parts[0] if len(parts) > 1 else ""
        if bin_dir is not None and top:
            target = staging / top
            (target / "bin").mkdir(parents=True, exist_ok=True)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                shutil.copy2(bin_dir / name, target / "bin" / name)
            for dll in bin_dir.glob("*.dll"):
                shutil.copy2(dll, target / "bin" / dll.name)
            _hoist_single_dir(staging, required)
            if _activate(staging, root, version, required):
                if report:
                    report("engine", None,
                           "引擎用的 ffmpeg 已随包就绪（同一构建，不再下载、不再问 GitHub）")
                return True
            shutil.rmtree(staging, ignore_errors=True)

    # 2) 随包的小件（bundle_runtime\<id>.zip）
    bundled = _bundle_zip(rid)
    if bundled is not None:
        try:
            _safe_extract(bundled, staging)
            _hoist_single_dir(staging, required)
            if _activate(staging, root, version, required):
                if report:
                    report("engine", None, f"{rid} 已随包就绪（不再下载）")
                return True
        except (OSError, ValueError, zipfile.BadZipFile):
            pass
        shutil.rmtree(staging, ignore_errors=True)

    # 3) 网络：走 app 自己的多镜像回退
    url = str(asset.get("url") or "")
    if not url or asset.get("digest_from"):
        return False
    archive = root / f"{version}.starryvt-download.zip"
    try:
        mirrors.fetch(url, archive, params, report, stage="engine", timeout=300,
                      sha256=want_sha, validate=zipfile.is_zipfile)
        _safe_extract(archive, staging)
        _hoist_single_dir(staging, required)
        ok = _activate(staging, root, version, required)
    except Exception as e:  # noqa: BLE001 - 预置失败就当没做，引擎会自己下
        if report:
            report("engine", None, f"（{rid} 预置没成功，交给引擎自己下载：{e}）")
        ok = False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
    return ok


def _activate_same_dir(root: Path, version: str, required: list[str]) -> bool:
    """文件已经齐了，只补一个 current.json"""
    tmp = root / "current.json.tmp"
    try:
        tmp.write_text(json.dumps({"current": version}), encoding="utf-8")
        os.replace(tmp, root / "current.json")
        return True
    except OSError:
        return False


def prefetch(params: dict, report=None) -> dict[str, bool]:
    """把引擎的必装资源提前放到磁盘上，返回 {资源 id: 是否已就绪}

    放在 `finesub setup` **之前**调用。ffmpeg 成功 = setup 阶段不会再出网。
    """
    specs = _specs()
    out: dict[str, bool] = {}
    for rid in PREFETCH_IDS:
        spec = specs.get(rid)
        if not spec:
            continue
        try:
            out[rid] = _prefetch_one(rid, params, spec, report)
        except Exception as e:  # noqa: BLE001 - 预置是加分项，绝不能拦住安装
            out[rid] = False
            if report:
                report("engine", None, f"（{rid} 预置跳过：{e}）")
    return out


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
