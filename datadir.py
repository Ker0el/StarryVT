#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""引擎数据目录（FINESUB_HOME）自检 + 自动改址

翻译引擎第一次跑任务时，要在自己的数据目录里建一个「小版本链接」目录
（Windows 的 junction）。有些机器上 Windows 会拒绝：

    error: Failed to create Python minor version link directory
      Caused by: 无法遍历路径，因为它包含不受信任的装入点。 (os error 448)

原因：这条路径上有一层 Windows 不信任的重解析点——OneDrive 的「文件按需」、
网盘客户端，或者把用户目录搬到别的盘的工具（电脑管家 / 360 / Dism++）留下的
junction。上游已知问题 astral-sh/uv#19616。注意：uv 的 Python 装在哪由引擎写死成
`<FINESUB_HOME>\\runtime\\python-builds`，所以**只能改 FINESUB_HOME**，从外面设
UV_PYTHON_INSTALL_DIR / UV_DATA_DIR 没用。

用户不该知道这些。所以开工前在这里试一下：默认目录建不了链接，就换一个能建的，
把结果写进设置（设置页看得见、诊断报告里有），用户无感。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from pathlib import Path

from job import no_window_kwargs

# 引擎自己报的「建链接被拒」特征（判断任务为什么失败，见 pipeline._translate_once）
SIGNS = ("os error 448", "不受信任的装入点", "untrusted mount point")

DRIVE_FIXED = 3
PROBE_DIR = ".starryvt-linkprobe"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_ATTRIBUTES = (0xFFFFFFFF, -1)

_CACHE: dict[str, bool] = {}


class MountBlocked(RuntimeError):
    """Windows 拒绝在数据目录里建目录链接（os error 448），换目录可解"""


def looks_blocked(blob: str) -> bool:
    """引擎输出里有没有「建链接被拒」的痕迹"""
    b = blob.lower()
    return any(k in b for k in SIGNS)


# ---------- 候选目录 ----------

def engine_default_home() -> Path:
    """引擎自己内置的默认数据目录（finesub 不认环境变量时用的那个）"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FineSub"


def default_home() -> Path:
    """我们给引擎选的数据目录。

    · 已经有引擎装在 `%LOCALAPPDATA%\\FineSub` → **继续用它**，别把用户的引擎抛下
    · 还没装过 → 跟「数据目录」走：`<数据目录>\\engine`
      （新用户的数据目录是 `<软件目录>\\data`，所以引擎也会落在软件目录里）
    """
    legacy = engine_default_home()
    try:
        if runtime_ready(legacy):
            return legacy
    except Exception:  # noqa: BLE001 - 探测失败就按没装处理
        pass
    try:
        import paths
        return paths.root() / "engine"
    except Exception:  # noqa: BLE001
        return legacy


def _app_dir() -> Path:
    import sys
    import tools
    if tools.is_packaged():
        return Path(sys.executable).parent
    return Path(__file__).parent


def _other_drives() -> list[Path]:
    """别的固定硬盘上的数据目录（U 盘/网络盘不放几个 GB 的模型）"""
    system = os.environ.get("SystemDrive", "C:").upper()
    out: list[Path] = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        if f"{letter}:" == system:
            continue
        root = f"{letter}:\\"
        try:
            if not os.path.exists(root):
                continue
            if ctypes.windll.kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
                continue
        except Exception:  # noqa: BLE001 - 探不到就当没有这块盘
            continue
        out.append(Path(root) / "StarryVT-FineSub")
    return out


def candidates(current: str = "") -> list[Path]:
    """按「越自然越好」排序的候选数据目录（去重）"""
    out: list[Path] = []
    if current:
        out.append(Path(current))
    out.append(default_home())                        # 数据目录\engine（新装）或老位置
    out.append(engine_default_home())                 # 引擎自己的默认（可能已经装在这儿）
    out.extend(_other_drives())                      # 换块盘，绕开 C 盘上的重解析点
    out.append(_app_dir() / "FineSub")               # 跟着程序走
    out.append(engine_default_home().parent / "StarryVT" / "FineSub")
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


# ---------- 探测 ----------

def reparse_points(directory: Path) -> list[Path]:
    """这条路径上有哪几层是「重解析点」（junction / 符号链接 / OneDrive 按需文件）

    os error 448 的原文是「路径不能遍历，因为它包含不受信任的装入点」——
    出问题的**不是目录本身，而是路径上穿过的那一层**。客户机实测：
    `C:\\Users\\x\\AppData\\Local\\FineSub` 和 `E:\\soft\\StarryVT` 都建不了链接，
    而同在 E 盘、直接落在盘根的 `E:\\DeliveryOptimization` 就行。
    所以挑目录前先看这一层，比事后试要准。
    """
    out: list[Path] = []
    parts = directory.parts[1:] if directory.anchor else directory.parts
    cur = Path(directory.anchor) if directory.anchor else Path()
    for part in parts:
        cur = cur / part
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(cur))
        except Exception:  # noqa: BLE001 - 查不了就当作没有
            break
        if attrs in INVALID_ATTRIBUTES:      # 不存在：后面的层更不存在
            break
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            out.append(cur)
    return out


def _is_app_dir(directory: Path) -> bool:
    """是不是程序目录或它的子目录 —— 卸载会连 {app} 一起删，运行时放这儿会被误删

    ⚠️ 例外：`paths.root()` 那个「数据目录」是**故意**放进软件目录的
    （新布局 `<app>\\data`），它里面的东西卸载时一起删是预期行为。
    """
    app = _app_dir()
    try:
        d = Path(os.path.normcase(str(directory)))
        a = Path(os.path.normcase(str(app)))
        try:
            import paths
            data = Path(os.path.normcase(str(paths.root())))
            if d == data or data in d.parents:
                return False
        except Exception:  # noqa: BLE001 - 读不到数据目录就按老规则来
            pass
    except (OSError, ValueError):
        return False
    return d == a or a in d.parents


def unusable_reason(directory: Path) -> str:
    """这个目录为什么不能用（空串 = 能用）——探测 + 已知失败记录"""
    if _is_app_dir(directory):
        return "是程序目录（卸载时会连它一起删）"
    marks = reparse_points(directory)
    if marks:
        return f"路径上有重解析点（网盘 / 搬家工具）：{marks[0]}"
    if os.path.normcase(str(directory)) in {os.path.normcase(p) for p in bad_homes()}:
        return "这台机器上建过链接失败（os error 448），已排除"
    if not junction_ok(directory):
        return "建不了目录链接"
    return ""


def junction_ok(directory: Path) -> bool:
    """这个目录里能不能建目录链接？建不了的话引擎装运行环境必然失败（os error 448）

    只做一次真实探测（建一个链接立刻删掉），结果记在内存里，同一进程内不重复探。
    注意：**探测通过 ≠ 引擎一定行**（实测有过反例，路径上那一层重解析点会让 uv
    自己的建链接失败而我们探测通过），所以 bad_homes() 里记着的目录一律排除。
    """
    key = os.path.normcase(str(directory))
    if key in _CACHE:
        return _CACHE[key]
    if reparse_points(directory):
        _CACHE[key] = False
        return False
    probe = directory / PROBE_DIR
    link = probe / "link"
    target = probe / "target"
    existed = directory.exists()
    ok = False
    try:
        probe.mkdir(parents=True, exist_ok=True)
        target.mkdir(exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True, **no_window_kwargs())
        # 建得出来、且能穿过去看一眼，才算真能用（有些机器建得出来但走不通）
        ok = r.returncode == 0 and os.path.isdir(link)
    except Exception:  # noqa: BLE001 - 任何异常都视为这个目录不可用
        ok = False
    finally:
        try:
            os.rmdir(link)        # 只删链接本身，不动目标
        except OSError:
            pass
        shutil.rmtree(probe, ignore_errors=True)
        if not existed:
            try:
                directory.rmdir()     # 只是探一下，别在人家盘上留空目录
            except OSError:
                pass
    _CACHE[key] = ok
    return ok


# ---------- 建不了链接的目录（记在设置里，下次别再挑它） ----------

def bad_homes() -> list[str]:
    """历史上真的建过链接失败的目录（引擎报 448 时记下来的）"""
    try:
        import settings as cfg
        got = cfg.load().get("finesub_bad_homes") or []
        return [str(p) for p in got if str(p).strip()]
    except Exception:  # noqa: BLE001 - 读不到当没有
        return []


def mark_bad(home: str) -> None:
    """记下这个目录不行：实测探针会在这种机器上误判，不能只靠探"""
    home = (home or "").strip()
    if not home:
        return
    key = os.path.normcase(home)
    known = bad_homes()
    if any(os.path.normcase(p) == key for p in known):
        return
    try:
        import settings as cfg
        cfg.save({"finesub_bad_homes": known + [home]})
        _CACHE[os.path.normcase(home)] = False
    except Exception:  # noqa: BLE001 - 记不住也不影响本次运行
        pass


def runtime_ready(home: Path) -> bool:
    """这个数据目录里已经装好引擎运行环境了吗（装过就别换地方，换 = 重下好几个 GB）"""
    try:
        return (home / "runtime" / "python" / "Scripts" / "python.exe").is_file()
    except OSError:
        return False


# ---------- 选择 ----------

def pick(params: dict, *, exclude=(), force: bool = False) -> str:
    """挑一个能用的数据目录，返回要传给引擎的 FINESUB_HOME（空串 = 用引擎默认）

    exclude: 已经失败过的目录（448 兜底重试时把当前这个排掉）
    force:   忽略「已有运行时」，重新挑
    """
    skip = {os.path.normcase(str(Path(p))) for p in exclude if p}
    current = (params.get("finesub_home") or "").strip()

    # 已经装好了就绝不搬：换目录等于让用户重新下载模型
    if not force:
        if current and runtime_ready(Path(current)):
            return current
        if not current:
            picked = default_home()
            if runtime_ready(picked):
                # 只有引擎**自己的**默认目录才用空串表示，否则必须显式传路径
                return "" if picked == engine_default_home() else str(picked)

    for cand in candidates(current):
        if os.path.normcase(str(cand)) in skip:
            continue
        if not unusable_reason(cand):
            # 同上：`""` 只代表「引擎自己的默认 %LOCALAPPDATA%\FineSub」
            return "" if cand == engine_default_home() else str(cand)
    return current          # 一个都不行：维持现状，让 448 的中文提示去兜底


def ensure(params: dict, report=None, *, exclude=(), force: bool = False,
           stage: str = "translate") -> str:
    """开工前调一次：挑目录、写进设置、给用户留一句人话日志。返回最终要用的目录"""
    old = (params.get("finesub_home") or "").strip()
    home = pick(params, exclude=exclude, force=force)
    params["finesub_home"] = home
    if home != old:
        try:
            import settings as cfg
            cfg.save({"finesub_home": home})
        except Exception:  # noqa: BLE001 - 设置写不进去也不影响本次运行（env 照样传）
            pass
        if report:
            if home:
                report(stage, None,
                       f"⚠ 系统默认目录里装不了引擎运行环境（os error 448 不受信任的装入点），"
                       f"已自动换到别的目录")
            else:
                report(stage, None, "引擎数据目录已改回系统默认位置")
    return home


def describe() -> list[str]:
    """给诊断报告用：各候选目录能不能用、为什么不能用（只探测，不留痕）"""
    out: list[str] = []
    for p in candidates():
        why = unusable_reason(p)
        out.append(f"{'可用' if not why else '❌ ' + why}: {p}")
    bad = bad_homes()
    if bad:
        out.append("这台机器上真报过 os error 448 的目录：" + "；".join(bad))
    marks = reparse_points(default_home())
    if marks:
        out.append("系统默认目录路径上的重解析点：" + "；".join(str(m) for m in marks))
    return out
