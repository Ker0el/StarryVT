#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务上下文：取消标志 + 子进程登记 + **进程退出时自动清理整棵进程树**

子进程是**子孙成群**的：我们起的 `finesub.exe` 下面还有 `uv.exe`、`python.exe`。
只杀直接子进程的话，孙子会活下来继续跑（继续下 torch、继续占着引擎数据目录里的
uv 缓存锁），下一次开工就撞锁——见下面 `guard()` 的说明。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading

# Windows: 让子进程不弹出控制台黑窗口（GUI 应用调控制台程序时必须）
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 让一个子进程脱离 job（重启自己的那条路必须要，见 updater.install_and_restart）
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_JOB_HANDLE = None
_JOB_LOCK = threading.Lock()


# ---------------- 进程树守护（Windows Job Object）----------------

class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),          # ULONG_PTR
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800


def guard() -> bool:
    """把本进程挂进一个「句柄一关就杀掉全部子孙」的作业对象；成功返回 True。

    **为什么非做不可**（2026-10-08 客户实测）：以前只有用户点「取消」才会杀子进程。
    用户关掉窗口（或者走到更新那条 `os._exit(0)`）时，`finesub.exe` 和它下面的
    `uv.exe` 全变成孤儿，继续下载、继续占着 `<引擎数据目录>\\cache\\uv\\...\\*.lock`。
    用户再打开软件跑一次，新的 uv 干等 300 秒拿不到锁然后失败：

        Failed to acquire lock on the distribution cache
        Timeout (300s) when waiting for lock on .../torch-2.11.0+cu128-....lock,
        is another uv process running?

    客户那天上午连着栽了两次（10:26、10:31），而界面只报一句「finesub 翻译失败」。

    挂上去之后**不需要任何清理代码**：本进程一消失（正常退出、崩溃、被 taskkill 杀掉
    都算），Windows 会把整个作业里的进程全部结束。子进程的子孙也自动在内——作业成员
    身份是**继承**的，这也是它比「启动完再 AssignProcessToJobObject」可靠的地方
    （后者有竞态：assign 之前子进程可能已经生了孙子，孙子就不在作业里了）。

    ⚠️ 代价：连「重启自己」的那条更新脚本也会被一起杀掉。所以作业要放开
    `BREAKAWAY_OK`，而那个 `cmd` 必须带 `CREATE_BREAKAWAY_FROM_JOB`
    （见 `updater.install_and_restart`）。**这两处必须成对改，改一处会把自动更新打死。**

    只在 Windows 上做；任何一步失败都当成没装（功能不受影响，只是退回旧行为）。
    """
    global _JOB_HANDLE
    if os.name != "nt":
        return False
    with _JOB_LOCK:
        if _JOB_HANDLE:
            return True
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.restype = ctypes.c_int
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_void_p, ctypes.c_uint32]
            k32.AssignProcessToJobObject.restype = ctypes.c_int
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.GetCurrentProcess.argtypes = []
            # 句柄必须按指针传：不声明 argtypes 时 ctypes 默认按 C int 传，
            # x64 上会被截成 32 位，CloseHandle 关错东西就是内存/句柄事故
            k32.CloseHandle.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK)
            if not k32.SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                return False
            if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
                # 已经在一个不允许嵌套的作业里（老系统 / 被别的启动器套着）：
                # 不是错误，退回旧行为即可。
                k32.CloseHandle(job)
                return False
            _JOB_HANDLE = job          # 故意不关：句柄一关，整棵树当场被杀
            return True
        except Exception:
            return False


def no_window_kwargs(**kw) -> dict:
    """给 subprocess 调用补上「不弹控制台」和「不给 stdin」两个标志

    stdin 必须关掉：ffmpeg 会读 stdin 做交互（q/+/−），stdin 是一个还开着的管道时
    它可能就卡在那儿不退出——从命令行启动、或者被更新脚本的 cmd 链拉起来时就会这样，
    表现为"压制到一半不动了"，而且换台机器/换个启动方式就复现不了。
    """
    if os.name == "nt":
        kw.setdefault("creationflags", CREATE_NO_WINDOW)
    kw.setdefault("stdin", subprocess.DEVNULL)
    return kw


class Cancelled(Exception):
    """用户取消任务"""


class Job:
    def __init__(self):
        guard()                      # 幂等；确保关窗口时不会留下孤儿进程树
        self.cancel = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        # 字幕检查暂停点：引擎出完原始字幕后停在这里，等用户改完点「继续翻译」。
        self.review_event = threading.Event()
        self.review_ready = False        # True = 正在暂停等用户确认
        self.review_kind = ""            # raw = 识别结果 / final = 译文
        self.review_path = ""            # 当前要检查的字幕路径（给界面显示）
        self.review_source = ""          # 原文路径（译文检查时做双语对照）
        self.review_continue_path = ""   # 用户确认时选定的字幕（编辑副本或原始）

    # ---- 取消检查 ----
    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def cancel_now(self):
        self.cancel.set()
        self.review_event.set()      # 暂停点也要立刻醒过来，否则取消会一直卡在 wait
        self.kill_all()

    # ---- 子进程 ----
    def spawn(self, cmd, **kw) -> subprocess.Popen:
        """启动一个受管的子进程（取消时会被 kill；不弹控制台窗口）"""
        self.check()
        try:
            p = subprocess.Popen(cmd, **no_window_kwargs(**kw))
        except FileNotFoundError as e:
            # 程序不存在时 Windows 只抛 [WinError 2] 且**不带文件名**，用户无从下手
            raise RuntimeError(
                f"找不到程序「{cmd[0]}」——它可能被杀毒软件隔离或安装不完整。"
                f"请重新安装本软件，或把该程序放到软件目录后重启。") from e
        with self._lock:
            self._procs.append(p)
        return p

    def run(self, cmd, **kw) -> subprocess.CompletedProcess:
        """阻塞式运行，返回 CompletedProcess（不抛 CalledProcessError）

        兼容 subprocess.run 风格的 capture_output/text 参数。
        """
        if kw.pop("capture_output", False):
            kw.setdefault("stdout", subprocess.PIPE)
            kw.setdefault("stderr", subprocess.PIPE)
        p = self.spawn(cmd, **kw)
        out, err = p.communicate()
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)

    def kill_all(self):
        """结束本次任务起的全部子进程（**连子孙一起**）"""
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                kill_tree(p.pid)
                try:
                    p.kill()             # taskkill 没成功时的兜底
                except Exception:
                    pass


def kill_tree(pid: int) -> bool:
    """按 PID 结束整棵进程树（Windows 用 taskkill /T，其它平台退回杀进程本身）。

    为什么不能只 `p.kill()`：直接子进程是 `finesub.exe`，真正在下载、真正握着
    uv 缓存锁的是它下面的 `uv.exe` / `python.exe`。父进程一死，这些孙子会被
    重新挂到系统上继续跑——用户看到的现象就是"明明取消了，下次开工却还是撞锁"。
    """
    if os.name == "nt":
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               **no_window_kwargs(capture_output=True, text=True,
                                                  encoding="utf-8", errors="replace",
                                                  timeout=15))
            return r.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, 9)              # type: ignore[attr-defined]
        return True
    except Exception:
        return False
