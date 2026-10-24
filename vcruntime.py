#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""微软 VC++ 运行库（MSVC 14.x）的检测与一键安装

## 为什么需要这个

PyTorch 的 Windows wheel **不自带** MSVC 运行库：`torch\\lib\\c10.dll` 运行时去
系统目录找 `MSVCP140.dll` / `VCRUNTIME140.dll` / `VCRUNTIME140_1.dll`。torch >= 2.9
是用 MSVC 14.42 编的，而 `c10.dll` 的静态初始化里有一个 `constexpr std::mutex`：

* 机器上的 `msvcp140.dll` **太旧**（< 14.40）→ 初始化例程直接失败；
* 机器上**没有** → 一样失败。

两种都只给出一句 `OSError: [WinError 1114] DLL 初始化例程失败 / Error loading
...\\torch\\lib\\c10.dll or one of its dependencies`。看着像 torch 坏了，其实
换一份运行库就好（pytorch#166628 / #196987 / #199107）。

## 2026-10-23 客户报障说明什么

那台机器已经 `Installed 90 packages in 20.89s`，最后验证 `import torch` 时炸在
c10.dll；引擎 `_install_locked` 一失败就 `_discard(staging)`，于是**每次重试都要
把整套依赖重装一遍**（torch 的 wheel 走 uv 缓存，但重建 venv + 解包落盘也要好
几分钟）—— 用户点了十几次，一次也没走完。

所以这一关必须在**下 torch 之前**过掉：`preflight()` 只读几个文件版本，几毫秒；
不过关就 `ensure()` 当场把官方运行库装上（会弹一次 UAC），再继续安装。

⚠️ 判不出来时**放行**（和 engine.check_space 一个态度）：宁可让它去试，
也别因为读不到版本把能用的机器挡在门外。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

import mirrors

#: 官方 x64 运行库安装包。地址是 **download.visualstudio.microsoft.com 的内容寻址
#: 路径**：路径里那段大写 sha256 就是文件本身的哈希 —— 内容一变地址就变，
#: 所以这个 URL 可以钉死（比 aka.ms 那个永远指向"最新"的短链稳）。
VC_URL = ("https://download.visualstudio.microsoft.com/download/pr/"
          "990c720a-46ce-4dc7-87b9-c2eb615d34b9/"
          "CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/"
          "VC_redist.x64.exe")
VC_SHA256 = "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"
VC_SIZE = 25635768

#: torch（MSVC 14.42 构建）要求的最低 msvcp140 版本。14.40 是 pytorch#196987
#: 里给出的分界：更旧的 msvcp140 会在 constexpr std::mutex 那一步把 c10.dll 弄崩。
MIN_MSVCP = (14, 40)

#: 缺了必然加载不了的几支（c10.dll 的导入表里就这几支，wheel 不带）
_NEEDED = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
           "vcruntime140_threads.dll")

#: 安装包已经装过时 Windows 会返回这两个码之一，都算成功
_OK_CODES = (0, 1638, 3010)


# ---------- 读版本 ----------

def _system32(name: str) -> Path:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return Path(root) / "System32" / name


def _file_version(path: Path) -> tuple[int, int, int, int] | None:
    """读一个 PE 的固定版本号（VS_FIXEDFILEINFO）；读不到返回 None

    用 version.dll 而不是第三方库：这块逻辑只有几十行，多带一个依赖不划算。
    ⚠️ 64 位上必须显式声明 argtypes/restype，否则指针会被截成 32 位。
    """
    if os.name != "nt":
        return None
    try:
        ver = ctypes.WinDLL("version", use_last_error=True)
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.c_void_p]
        ver.GetFileVersionInfoW.restype = wintypes.BOOL
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        ver.VerQueryValueW.restype = wintypes.BOOL

        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        n = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)):
            return None
        if not ptr.value:
            return None
        # VS_FIXEDFILEINFO：DWORD dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS
        dw = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD))
        ms, ls = int(dw[2]), int(dw[3])
        return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:  # noqa: BLE001 - 读不到按「判不了」处理
        return None


def versions() -> dict[str, tuple[int, int, int, int] | None]:
    """System32 里那几支 DLL 的版本（不存在或读不出 → None）"""
    out: dict[str, tuple[int, int, int, int] | None] = {}
    for name in _NEEDED:
        p = _system32(name)
        out[name] = _file_version(p) if p.is_file() else None
    return out


def _fmt(v: tuple[int, int, int, int]) -> str:
    return ".".join(str(x) for x in v[:3])


def summary() -> str:
    """一行给人看的现状（诊断报告和安装日志都用它）"""
    v = versions()
    parts = []
    for name in _NEEDED:
        ver = v[name]
        if not _system32(name).is_file():
            parts.append(f"{name}=缺失")
        else:
            parts.append(f"{name}={_fmt(ver) if ver else '版本未知'}")
    return "；".join(parts)


def problem() -> str:
    """运行库有问题时返回一句人话，正常/判不了返回空串"""
    if os.name != "nt":
        return ""
    v = versions()

    if not _system32("msvcp140.dll").is_file():
        return "系统里没有 MSVCP140.dll（微软 VC++ 运行库没装）"
    ms = v.get("msvcp140.dll")
    if ms is None:
        # 文件在、版本读不出来：不是「旧」，是「判不了」——放行
        return ""
    if tuple(ms[:2]) < MIN_MSVCP:
        return (f"系统里的 MSVCP140.dll 是 {_fmt(ms)}，"
                f"torch 要求 {MIN_MSVCP[0]}.{MIN_MSVCP[1]} 以上")

    for name in ("vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll"):
        if not _system32(name).is_file():
            return f"缺少 {name}（微软 VC++ 运行库版本太旧）"
    return ""


def check(report=None, stage: str = "engine") -> bool:
    """只检查、不装。日志里留一行现状，方便下次看报告时直接定位。"""
    if os.name != "nt":
        return True
    if report:
        report(stage, None, f"微软运行库：{summary()}")
    why = problem()
    if why and report:
        report(stage, None, f"⚠ {why}")
    return not why


# ---------- 装 ----------

class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _run_elevated(exe: Path, args: str) -> tuple[bool, int]:
    """以管理员身份跑安装包并等它结束，返回 (是否启动, 退出码)

    为什么不用 subprocess：vc_redist.exe 的清单是 requireAdministrator，
    CreateProcess 会直接回 ERROR_ELEVATION_REQUIRED(740)，**不会弹 UAC**。
    只有 ShellExecuteExW + runas 才会把那一次授权弹窗交给用户。
    """
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = str(exe)
    sei.lpParameters = args
    sei.nShow = 0                                    # SW_HIDE：安装包自己有 splash
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        return False, ctypes.get_last_error() or 0
    code = -1
    try:
        if sei.hProcess:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(wintypes.DWORD)]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            WAIT_TIMEOUT = 0x00000102
            if k32.WaitForSingleObject(sei.hProcess, 15 * 60 * 1000) == WAIT_TIMEOUT:
                return True, -1
            out = wintypes.DWORD()
            if k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(out)):
                code = int(out.value)
    finally:
        if sei.hProcess:
            ctypes.WinDLL("kernel32").CloseHandle(sei.hProcess)
    return True, code


def ensure(params: dict | None = None, report=None, stage: str = "engine") -> bool:
    """运行库正常 → True；不正常就当场装官方的，装完再验一次。

    返回 False 时**调用方必须停手**：继续装 torch 只会白下 2.6 GB。
    """
    if os.name != "nt":
        return True
    if not problem():
        check(report, stage)
        return True

    why = problem()
    if report:
        report(stage, None,
               f"⚠ {why} —— torch 的 c10.dll 会加载失败，先补运行库再继续。")
        report(stage, None, "正在下载微软 VC++ 运行库（约 25 MB）…")
    dest = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT" / "bin" / "VC_redist.x64.exe"
    try:
        mirrors.fetch(VC_URL, dest, params or {}, report, stage=stage,
                      timeout=300, sha256=VC_SHA256)
    except Exception as e:  # noqa: BLE001
        if report:
            report(stage, None, f"❌ 运行库下载失败：{e}")
            report(stage, None, "可以手动装一次 https://aka.ms/vc14/vc_redist.x64.exe 再重试。")
        return False

    if report:
        report(stage, None, "正在安装运行库（这一步会弹一次「是否允许更改」的授权窗口）…")
    started, code = _run_elevated(dest, "/install /quiet /norestart")
    if not started:
        if report:
            if code == 1223:                     # ERROR_CANCELLED
                report(stage, None, "❌ 你取消了授权，运行库没装上。装引擎需要它。")
            else:
                report(stage, None, f"❌ 运行库安装程序没能启动（错误码 {code}）。")
            report(stage, None, "可以手动装一次 https://aka.ms/vc14/vc_redist.x64.exe 再重试。")
        return False

    # 安装包把文件换掉后，Windows 可能要重启才生效；给它一点时间再读一次版本
    for _ in range(10):
        if not problem():
            break
        time.sleep(1.0)
    if not problem():
        if report:
            report(stage, None, f"✅ 运行库已就绪（{summary()}）")
        return True

    if report:
        if code in _OK_CODES:
            report(stage, None,
                   "⚠ 运行库已经装上，但要**重启电脑**之后才生效。"
                   "重启后回来点「重新试一次」，已下好的部分不会重下。")
        else:
            report(stage, None,
                   f"❌ 运行库安装没成功（退出码 {code}）。"
                   f"请手动装一次 https://aka.ms/vc14/vc_redist.x64.exe 再重试。")
    return False
