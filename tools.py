#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外部程序定位：ffmpeg / ffprobe / yt-dlp

本软件自带这三个程序（放在程序目录的 bin\\ 下，随安装包一起分发），
所以正常情况下不需要用户自己装、也不需要配 PATH。查找顺序：

    1. 用户级目录    %APPDATA%\\StarryVT\\bin\\   （软件自动更新下来的新版本放这儿）
    2. 随包分发目录  <程序目录>\\bin\\           （打包后即 _internal\\bin\\）
    3. 程序目录下的 bin\\ 与程序目录本身          （用户手动放一份进去也能用）
    4. 系统 PATH                                （开发机 / 绿色版兜底）

第 1 条排在最前面是**故意**的：装在只读位置时软件没法就地替换随包的那份，
只能把新版落到用户目录（见 ytdlp.py 的 _pick_target）。那个目录本来就有
（engine.py 往里放 uv.exe），多一个 yt-dlp.exe 不影响任何东西。

**为什么要有这个模块**：以前直接写 `subprocess.Popen(["ffmpeg", ...])`，
用户机器上没有 ffmpeg 时 Windows 抛的是
`FileNotFoundError: [WinError 2] 系统找不到指定的文件。`
——**不带任何文件名**，用户和客服都看不出缺的是什么。现在改成先定位到绝对路径，
找不到就抛可读的中文提示（见 MissingTool）。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path

# 名称 → 给用户看的说明
# 给用户看的是**功能名**，不是程序名（用户 2026-10-14：别让人看见技术栈被抄）
DESC = {
    "ffmpeg": "视频处理组件",
    "ffprobe": "时长读取组件",
    "yt-dlp": "视频下载组件",
}

# 随包分发、启动时要自检的程序
REQUIRED = ("ffmpeg", "ffprobe", "yt-dlp")

_cache: dict[str, str | None] = {}


def long_path(p) -> Path:
    """把 8.3 短路径换成真实长路径（`D:\\AI_PRO~1` → `D:\\AI_Project`）；判不了就原样返回

    ★ 为什么需要（2026-10-24）：Windows 在启用 8dot3 的盘上给每个目录准备了短名，
      而 `sys.executable` 返回的是**进程启动时用的那条路径**。用短路径起的进程，
      `app_dir()` / `paths.app_dir()` 就都是短的；这串再被写进设置（`finesub_home`）
      就永远回不来了 —— 本机日志里 `data\\engine` 一直是 `D:\\AI_PRO~1\\…`，
      重启、重装都没用，只有把文件里那串改掉才行。

    路径不存在（移动硬盘拔了之类）时 GetLongPathNameW 会失败，这时原样返回，
    调用方拿到的还是它传进来的那个值，行为不变。
    """
    try:
        path = Path(p)
    except TypeError:
        return p
    if os.name != "nt":
        return path
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                         wintypes.DWORD]
        k32.GetLongPathNameW.restype = wintypes.DWORD
        s = str(path)
        need = k32.GetLongPathNameW(s, None, 0)
        if not need:
            return path
        buf = ctypes.create_unicode_buffer(need)
        if not k32.GetLongPathNameW(s, buf, need):
            return path
        return Path(buf.value)
    except Exception:  # noqa: BLE001 - 取不到长路径就用原来那个，绝不抛
        return path


def is_packaged() -> bool:
    """现在跑的是「打包后的程序」还是「源码直接跑」。

    ★★ **不能只看 `sys.frozen`** —— 这两个打包器的行为不一样：

    | 打包器 | `sys.frozen` | 别的标记 |
    |---|---|---|
    | PyInstaller（**开发版后端**用的） | **真** | `sys._MEIPASS` |
    | Nuitka（**生产版后端**用的） | **不设**（假值！） | 主模块里的 `__compiled__` |

    所以 `getattr(sys, "frozen", False)` 在**开发机上永远为真、在用户机器上永远为假**，
    任何「打包时才做 / 开发时才做」的分支都会**静默走反**，而且本地怎么测都测不出来
    （开发版是 PyInstaller、生产版是 Nuitka）。

    2026-10-15 实测事故：用户点「立即更新」→ 下载 162MB、sha256 校验通过 → 然后什么都没有，
    界面永远停在 100%。根因就是 updater 以为自己在开发模式，把安装包当 GUI 程序打开、
    不写更新脚本、不静默安装，然后自己 `os._exit(0)` 走人。

    ⚠️ **以后凡是「打包环境" 的判断，一律用这个函数**，别再写 `getattr(sys, "frozen", False)`。
    """
    if getattr(sys, "frozen", False):        # PyInstaller / cx_Freeze / py2exe
        return True
    if "__compiled__" in globals():          # Nuitka：本模块被编译时
        return True
    main = sys.modules.get("__main__")       # Nuitka 把 __compiled__ 放主模块命名空间里
    return main is not None and hasattr(main, "__compiled__")


def app_dir() -> Path:
    """程序目录（打包后是 StarryVT.exe 所在目录，开发时是源码目录）

    打包态要过一遍 long_path：程序若是用 8.3 短路径启动的（`D:\\AI_PRO~1\\…`），
    `sys.executable` 就是短的，跟着算出来的数据目录也会是短的。见 long_path 的说明。
    """
    if is_packaged():
        return long_path(Path(sys.executable)).parent
    return Path(__file__).parent


def bundle_dir() -> Path:
    """随包资源目录（打包后 PyInstaller 的 _internal，开发时同程序目录）"""
    if is_packaged():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).parent


def user_bin_dir() -> Path:
    """用户级程序目录：%APPDATA%\\StarryVT\\bin（engine.py 也用它放 uv.exe）

    软件自动更新下来的程序落在这儿——装在只读位置时随包那份改不动，只能换个
    地方放（见 ytdlp.py 的 _pick_target）。目录已经存在了，多放一个 exe 不影响
    engine.py（它是按文件名找 uv.exe 的）。
    """
    import os
    return Path(os.environ.get("APPDATA") or Path.home()) / "StarryVT" / "bin"


def _search_dirs() -> list[Path]:
    app = app_dir()
    dirs = [user_bin_dir(), bundle_dir() / "bin", app / "bin", app]
    if not is_packaged():
        dirs.append(app / ".tools" / "bin")   # 开发时的暂存目录（.gitignore）
    out: list[Path] = []
    for d in dirs:                             # 去重且保持顺序
        if d not in out:
            out.append(d)
    return out


def find(name: str) -> str | None:
    """定位程序，返回绝对路径；找不到返回 None（结果缓存，refresh() 可清）"""
    if name in _cache:
        return _cache[name]
    hit: str | None = None
    for d in _search_dirs():
        cand = d / f"{name}.exe"
        try:
            if cand.is_file():
                hit = str(cand)
                break
        except OSError:
            continue
    if not hit:
        hit = shutil.which(name)
    _cache[name] = hit
    return hit


def path(name: str) -> str:
    """定位程序，找不到就抛 MissingTool（中文、可照做）"""
    p = find(name)
    if not p:
        raise MissingTool(name)
    return p


def refresh() -> None:
    """清缓存（用户中途补放了程序 / 装完引擎后重新自检）"""
    _cache.clear()


def missing() -> list[str]:
    """自检：返回缺失的程序名列表"""
    return [n for n in REQUIRED if not find(n)]


def self_check() -> dict:
    """自检结果（给界面/诊断报告用）"""
    found = {n: find(n) or "" for n in REQUIRED}
    miss = [n for n, p in found.items() if not p]
    return {"found": found, "missing": miss, "dirs": [str(d) for d in _search_dirs()]}


class MissingTool(RuntimeError):
    """缺少外部程序——消息面向用户，直接可显示（可一次报多个）"""

    def __init__(self, name: str | list[str]):
        names = [name] if isinstance(name, str) else list(name)
        self.names = names
        what = "、".join(DESC.get(n, n) for n in names)
        where = app_dir() / "bin"
        # 单行文案：状态横幅只有一行，多行会挤成一片
        super().__init__(
            f"缺少运行组件：{what}。本软件自带这些程序，正常应位于 {where}；"
            f"可能被杀毒软件隔离、或安装不完整——请检查杀毒软件的隔离区并恢复，"
            f"或重新安装本软件（也可以自行下载同名程序放到该目录后重启）。"
        )
