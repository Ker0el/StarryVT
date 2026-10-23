#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""翻译引擎（finesub）的检测与一键安装

分三步：
  1) 找 uv（Python 工具管理器）—— 没有就下载独立版到 %APPDATA%\\StarryVT\\bin
  2) `uv tool install finesub` 安装引擎本体
  3) 引擎首次运行时会自行下载运行时与模型（约 5GB，进度由流水线日志呈现）

所有网络访问都走用户配置的代理（GitHub / astral.sh 在国内需代理）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from job import Job, no_window_kwargs
import datadir
import engine_mirror
import mirrors
import tools
import wheels


class EngineError(RuntimeError):
    """**消息本身就是给人看的人话**的错误。

    区别在展示：普通异常会被拼成 `RuntimeError: xxx` 塞进界面（`type(e).__name__` 那一段），
    对这个错误就**只显示消息本身** —— 用户不需要知道它是个什么类型的异常。
    见 `app.Api._install_engine` 里那一行 isinstance。
    """

BIN_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT" / "bin"
# ★ 钉死版本，**别改回 `/releases/latest/download/`**（2026-10-15）：
#   · `latest` 是个**会动的地址** —— 上游一发新版，同一段代码请求到的字节就变了。
#     那样一来 sha256 没法钉（钉了迟早对不上，而且是在所有来源上一起失败）。
#   · 钉死之后，镜像 / GitHub 直连两边下到的必然是同一份，
#     `sha256` 才是个有意义的判据（能当场看出镜像给的是不是错误页）。
#   ⚠️ 换版本要**同时**改这两处：这里的 URL、下面的 SHA256。
UV_ZIP_URL = "https://github.com/astral-sh/uv/releases/download/0.13.1/uv-x86_64-pc-windows-msvc.zip"
UV_ZIP_SHA256 = "3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672"
TUNA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: 翻译引擎的版本范围（随包 wheelhouse 和在线安装共用这一个字符串）
ENGINE_REQ = "finesub>=0.6,<0.7"


# ---------- 检测 ----------

_UV_OK: dict[str, bool] = {}


def uv_works(exe: str) -> bool:
    """这个 uv.exe 真的能跑吗（结果按路径缓存 —— 一次运行里它不会变）

    ★ 为什么必须验（2026-10-16 查出来的**永久卡死**）：
      以前 `find_uv()` **只看文件在不在**。而装到一半断电 / 被任务管理器强杀时，
      `%APPDATA%\\StarryVT\\bin\\uv.exe` 会留下一个**截断的文件**。
      之后每一次装引擎都拿它去跑 → 每次都失败 → 而且**永远不会重新下载**
      （因为「已经有了」）→ 用户看到的是一句看不懂的 uv 报错，
      自己怎么重试都好不了，只能手动去删那个文件。
      这和「中断后下次能接着装」是同一件事的两面：**判据不能只看存在。**
    """
    key = str(exe)
    if key in _UV_OK:
        return _UV_OK[key]
    ok = False
    try:
        r = subprocess.run([exe, "--version"],
                           **no_window_kwargs(capture_output=True, text=True,
                                              encoding="utf-8", errors="replace", timeout=20))
        ok = r.returncode == 0 and "uv" in ((r.stdout or "") + (r.stderr or "")).lower()
    except Exception:  # noqa: BLE001 - 起不来 / 超时 / 被杀，都算不能用
        ok = False
    _UV_OK[key] = ok
    return ok


def find_uv() -> str | None:
    """找 uv：PATH > 常见安装位置 > 应用自己的 bin；**每个候选都要真能跑才算数**"""
    p = shutil.which("uv")
    if p and uv_works(p):
        return p
    for c in (BIN_DIR / "uv.exe", Path.home() / ".local" / "bin" / "uv.exe",
              Path.home() / ".cargo" / "bin" / "uv.exe"):
        if c.exists() and uv_works(str(c)):
            return str(c)
    return None


def find_finesub(exe_hint: str = "") -> str | None:
    """找 finesub 引擎：设置里的自定义路径 > PATH > 常见位置"""
    if exe_hint:
        p = Path(exe_hint)
        if p.exists():
            return str(p)
    p = shutil.which(exe_hint or "finesub")
    if p:
        return p
    # uv 允许把工具的可执行文件放到自定义目录（UV_TOOL_BIN_DIR）；用户设了它、
    # 又没加进 PATH 时，刚装好的引擎会找不到。先看这里，再看两个默认位置。
    tool_bin = os.environ.get("UV_TOOL_BIN_DIR", "").strip()
    if tool_bin:
        c = Path(tool_bin) / "finesub.exe"
        if c.exists():
            return str(c)
    for c in (Path.home() / ".local" / "bin" / "finesub.exe",
              BIN_DIR / "finesub.exe"):
        if c.exists():
            return str(c)
    return None


def status(exe_hint: str = "") -> dict:
    """引擎状态：{ready, engine_path, uv_path, uv_ready, runtime_ready, bin_dir}

    ★ `ready` 必须是「本体 + 运行环境都在」（2026-10-23 客户报障）：
      以前只看 finesub.exe 在不在，于是 `finesub setup` 失败（常见是 GitHub API
      限流导致 ffmpeg 装不上）后，首页照样打勾、按钮变成「进入本地翻译」，
      用户一进去就撞「引擎没装好」。运行环境才是能不能翻译的分界线。
    """
    fb = find_finesub(exe_hint)
    uv = find_uv()
    try:
        import datadir
        import settings as cfg
        home = (str((cfg.load() or {}).get("finesub_home") or "").strip()
                or str(datadir.default_home()))
        runtime_ok = datadir.runtime_ready(Path(home))
    except Exception:  # noqa: BLE001 - 状态查询不该因为读配置失败而崩
        runtime_ok = False
    return {
        "ready": bool(fb) and runtime_ok,
        "ok": bool(fb) and runtime_ok,
        "engine_path": fb or "",
        "uv_path": uv or "",
        "uv_ready": bool(uv),
        "runtime_ready": runtime_ok,
        "bin_dir": str(BIN_DIR),
    }


# ---------- 安装 ----------

def _proxy_env(params: dict) -> dict:
    env = dict(os.environ)
    proxy = mirrors.effective_proxy(params or {})
    if proxy:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    else:
        # 没有可用代理：清掉继承来的代理变量并强制直连。否则子进程会去读 Windows
        # 系统代理，而用户关掉代理软件后那个端口是死的 → WinError 10061（2026-10-21 客户报障）。
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = env["no_proxy"] = "*"
    # 没有代理的机器：uv 自己也要从 GitHub 拿 Python，这里换成国内加速镜像
    env.update(mirrors.env(params))
    return env


def uv_env(params: dict) -> dict:
    """给 uv 子进程用的环境：代理 + GitHub 加速前缀 + Python 下载镜像 + UTF-8 输出。

    本地语音克隆（localclone.py）也走 uv，别再各自拼一份 —— 2026-10-19 那边
    调了一个不存在的 `_uv_env`，把「下载本地语音克隆」整个卡死在第一步。
    """
    env = _proxy_env(params)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def install_uv(params: dict, job: Job, report) -> str:
    """下载 uv 独立版并解压到 %APPDATA%\\StarryVT\\bin，返回 uv.exe 路径"""
    # 先认我们自己那份：PATH 上可能有用户装的老 uv，不一定支持
    # `uv tool install "finesub>=0.6,<0.7"` 这种写法。随包这份是测过的 0.13.1。
    bundled = BIN_DIR / "uv.exe"
    if bundled.is_file() and uv_works(str(bundled)):
        report("engine", None, "运行环境已就绪")
        return str(bundled)
    existing = find_uv()
    if existing:
        report("engine", None, f"运行环境已就绪")
        return existing
    # 走到这儿有两种可能：真没有，或者**有但跑不起来** ——
    # 上次装到一半断电/被强杀，会在 bin 里留下一个**截断的 uv.exe**。
    # 旧的留着不要紧（下面解压会整个覆盖），但得跟用户说一声，
    # 否则他只会看到「怎么又在重新下载」。
    if (BIN_DIR / "uv.exe").exists():
        report("engine", None, "上次的运行环境没装完整，重新装一遍…")

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = BIN_DIR / "uv.zip"
    # uv 本体放在 GitHub 上：直接走 GitHub，失败时再试公共加速镜像
    # （顺序由 mirrors._candidate_urls 决定）。
    # ⚠️ validate 必须传：镜像限流时是**用 200 回一个 HTML 错误页**的，不验就会
    #    拿网页去解压 —— 用户看到的只有一句 `BadZipFile: File is not a zip file`，
    #    完全看不出是镜像的问题（2026-10-15 客户报障）。
    mirrors.fetch(UV_ZIP_URL, zip_path, params, report, stage="engine", timeout=180,
                  sha256=UV_ZIP_SHA256, validate=zipfile.is_zipfile)

    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(BIN_DIR)
    except zipfile.BadZipFile as e:
        # 正常走不到这儿（fetch 已经验过了），兜一句人话，别把 zipfile 的原文抛给用户
        raise RuntimeError(
            f"下载到的运行环境压缩包是坏的（{e}）。\n"
            f"请重新点一次「一键安装引擎」；如果一直这样，"
            f"去「设置 → GitHub 加速镜像」把它关掉再试（关掉后走官方源直连）。")
    finally:
        zip_path.unlink(missing_ok=True)

    uv = BIN_DIR / "uv.exe"
    if not uv.exists():
        # 压缩包内可能有子目录
        cands = list(BIN_DIR.rglob("uv.exe"))
        if not cands:
            raise RuntimeError("解压后没找到 uv.exe")
        uv = cands[0]
    # 刚解压出来的这份还没验过；把缓存清掉，让下一次 find_uv() 重新判断 ——
    # 不清的话，如果这个路径之前被判过「不能用」，它会一直被跳过（明明已经修好了）。
    _UV_OK.clear()
    report("engine", 100, f"运行环境就绪")
    return str(uv)


def _process_snapshot() -> dict[int, tuple[str, int]]:
    """当前全部进程：{pid: (exe 文件名, 父进程 pid)}（只用 Win32，毫秒级）

    ⚠️ 别用 wmic：Win11 上经常没有；tasklist 又拿不到父进程号 —— 而这次恰恰要
      靠父进程判断「这个 finesub 是不是另一个还开着的星译在用」。
    """
    if os.name != "nt":
        return {}
    import ctypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32),
                    ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", ctypes.c_uint32),
                    ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * 260)]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)      # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return {}
        out: dict[int, tuple[str, int]] = {}
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                out[int(pe.th32ProcessID)] = (str(pe.szExeFile), int(pe.th32ParentProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return out
    except Exception:  # noqa: BLE001 - 查不出来就当没有（退回 tasklist 那条路）
        return {}


def _finesub_pids() -> list[int]:
    """在跑的 finesub.exe 的 PID（拿不到进程快照时退回 tasklist）"""
    snap = _process_snapshot()
    me = os.getpid()
    if snap:
        return [pid for pid, (name, _ppid) in snap.items()
                if pid != me and name.lower() == "finesub.exe"]
    if os.name != "nt":
        return []
    try:
        r = subprocess.run(["tasklist", "/nh", "/fo", "csv", "/fi", "IMAGENAME eq finesub.exe"],
                           **no_window_kwargs(capture_output=True, text=True,
                                              encoding="utf-8", errors="replace",
                                              timeout=15))
    except Exception:  # noqa: BLE001
        return []
    pids: list[int] = []
    for line in (r.stdout or "").splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) < 2 or parts[0].strip('" ').lower() != "finesub.exe":
            continue
        try:
            pids.append(int(parts[1]))
        except ValueError:
            pass
    return pids


def busy_processes() -> list[str]:
    """还有别的 finesub 进程在跑吗？返回 ["finesub.exe (PID 1234)", ...]

    **为什么查这个**：关软件时没杀干净的话，上一轮的引擎进程会继续下载、继续握着
    `<引擎数据目录>\\cache\\uv\\...\\*.lock`。用户再开一轮，新的进程干等 300 秒锁超时
    然后失败——2026-10-08 客户一上午栽了两次。与其让他白等 5 分钟，不如开局就讲清楚。

    只认 `finesub.exe`：这个名字不会有别人用，判错的代价（拦住一个正常的任务）
    比漏判大得多。`uv.exe` 就不好说了——用户自己的别的工具也可能用它，所以只写进提示语。
    """
    return [f"finesub.exe (PID {pid})" for pid in _finesub_pids()]


def _orphan_leftovers(pids: list[int]) -> tuple[list[int], list[int]]:
    """把 finesub 分成「没人管了的」和「另一个还开着的星译在用的」

    用户 2026-10-21 的报障：后台留着 finesub，任务管理器里**根本杀不掉**。
    与其让他去折腾任务管理器，不如我们自己收掉 —— 但必须分清是谁的：

      · 父进程链上还有**另一个活着的星译**（StarryVT.exe / StarryVt.exe）→ 别动，
        那是用户另开的窗口正在翻译；
      · 父链已经断了（原来的星译早退出了）→ 孤儿，收掉；
      · 父链走到本进程 → 我们自己上一轮留下的（取消了但没杀干净），收掉。
    """
    snap = _process_snapshot()
    if not snap:
        return list(pids), []          # 看不到父子关系时按老行为：都算残留
    me = os.getpid()
    orphans: list[int] = []
    alive_elsewhere: list[int] = []
    for pid in pids:
        cur = snap.get(pid, ("", 0))[1]
        mine = False
        other = False
        seen: set[int] = set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur == me:
                mine = True
                break
            info = snap.get(cur)
            if not info:
                break                  # 父进程已经没了 → 孤儿
            name = info[0].lower()
            if name.startswith("starryvt"):
                other = True
                break
            if name in ("python.exe", "pythonw.exe"):
                # 开发模式 / 用户自己用脚本起的，别替别人杀
                other = True
                break
            cur = info[1]
        if other and not mine:
            alive_elsewhere.append(pid)
        else:
            orphans.append(pid)
    return orphans, alive_elsewhere


def kill_leftovers(report=None, stage: str = "translate") -> dict:
    """结束「没主人」的 finesub 进程（另一个还开着的星译在用的不动）。

    返回 {killed, skipped, remaining, changed}：
      killed    这次发过结束命令的 PID
      skipped   是另一个星译实例的，没动
      remaining 结束之后**还在**的 PID（杀不掉的）
      changed   是不是真的动过手（调用方据此决定要不要再查一遍）
    """
    import time
    pids = _finesub_pids()
    if not pids:
        return {"killed": [], "skipped": [], "remaining": [], "changed": False}

    from job import kill_tree
    orphans, skipped = _orphan_leftovers(pids)
    if skipped and report:
        report(stage, None,
               "还有另一个星译窗口正在用翻译引擎，没动它：" +
               "、".join(f"PID {p}" for p in skipped))
    killed: list[int] = []
    for pid in orphans:
        if kill_tree(pid):
            killed.append(pid)
        if report:
            report(stage, None, f"已结束残留的引擎进程 PID {pid}")
    if killed:
        # taskkill 返回成功不代表进程立刻消失（要等内核回收），轮询等它真的走
        for _ in range(20):                      # 最多 5 秒
            if not set(killed) & set(_finesub_pids()):
                break
            time.sleep(0.25)
    remaining = [p for p in _finesub_pids() if p in set(orphans)]
    return {"killed": killed, "skipped": skipped, "remaining": remaining,
            "changed": bool(killed)}


# ---------- 空间检查 ----------

# 装引擎这一步至少要留多少空间。实测依据（本机引擎数据目录跑满之后 15 GB）：
#   · torch 那一套解包后 ~2.8 GB（下载 2.6 GB，uv 的下载缓存还会再占一份）
#   · 语音运行时（ffmpeg / MinGit / uv / Python）~0.3 GB
#   · 第二模型校验权重 1.5 GB
#   · 人声分离模型 0.6 GB
#   合计落盘约 5.2 GB，算上 uv 缓存的中间副本，留到 10 GB 才稳。
# ⚠️ 第一次翻译还要再下约 6 GB 的语音模型 —— 所以 10~20 GB 之间会「装得完引擎、
#    但第一次翻译时又不够」，那种情况见 check_space 的第二档提示。
MIN_FREE_GB = 10.0
COMFY_FREE_GB = 20.0


def _free_gb(path: str) -> float:
    """那个路径所在分区还剩多少 GB（盘符解析交给 Path.anchor）"""
    anchor = Path(path).anchor or path
    return shutil.disk_usage(anchor).free / (1024 ** 3)


def check_space(params: dict, report=None) -> None:
    """空间不够就抛一句人话，**当场停手**

    ★ 为什么要硬拦（2026-10-16 定的）：以前磁盘这项只出现在自检报告里、而且被归成
      「要你自己处理」，**一键初始化会直接跳过它，照样开始下 4 GB**。
      下到一半把盘写满的后果不只是「装不上」——
      Windows 自己也要空间（临时文件、页面文件），盘满了整台机器都会开始抽风，
      而用户看到的只是一句看不懂的报错。
      所以判据放在**动手之前**，不够就一个字都不下。

    ⚠️ 读不到剩余空间时**放行**（判据失灵宁可让它试，别把人挡在门外）。
    """
    home = (params.get("finesub_home") or "").strip() or str(datadir.default_home())
    try:
        free = _free_gb(home)
    except OSError:
        return
    if report:
        report("engine", None, f"引擎数据目录：{home}（剩余 {free:.1f} GB）")
    if free < MIN_FREE_GB:
        # 行里只放一句短的（列表那一行放不下长句），完整的话走日志
        if report:
            report("engine", None,
                   f"❌ 磁盘空间不够：{home} 只剩 {free:.1f} GB，装引擎至少要 {MIN_FREE_GB:.0f} GB")
            report("engine", None,
                   "腾出空间，或到「设置 → 引擎数据目录」换一个空间大的盘，再重新点一次。")
        raise EngineError(
            f"磁盘空间不够：那个分区只剩 {free:.1f} GB（至少要 {MIN_FREE_GB:.0f} GB）。"
            f"腾出空间、或去设置里换个盘，再点一次。")
    if free < COMFY_FREE_GB and report:
        # 不拦 —— 装引擎这一步够用，只是提醒第一遍翻译会再下一批模型
        report("engine", None,
               f"（提醒：这个分区只剩 {free:.1f} GB，第一次翻译还要再下约 6 GB 的语音模型）")


def _keep_awake(on: bool) -> None:
    """装引擎期间**别让系统睡觉**。

    这一步要连着下几个 GB，慢的机器十几分钟到半小时。中途睡着会把网断掉、
    下载中断，用户看到的是一个说不清的失败（「怎么又装不上了」）。
    `ES_SYSTEM_REQUIRED` 只是不让系统睡眠，**屏幕该黑还是黑**，比「保持屏幕常亮」礼貌。

    ⚠️ 这个状态是**按线程**的，装完（或那个线程结束）会自动还原，
    所以异常路径不用特意兜底。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:  # noqa: BLE001 - 挡不住睡眠不该影响安装
        pass


def has_nvidia() -> bool:
    """这台机器有没有 NVIDIA 显卡/驱动。

    引擎会按「有没有 CUDA 设备」自己决定跑 GPU 还是 CPU，
    所以**没 N 卡也能用**，只是慢一两个数量级。但运行时下的是 CUDA 版 torch（2.6GB），
    对没 N 卡的机器是白下 —— 所以提前说一句，别让人以为装坏了。
    """
    if os.name != "nt":
        return True                       # 判不了就别乱说
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        r = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"],
                           **no_window_kwargs(capture_output=True, text=True,
                                              encoding="utf-8", errors="replace", timeout=15))
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return False


def install_finesub(params: dict, job: Job, report) -> str:
    """一键安装：uv → finesub 引擎本体（运行时不在此步，首次跑任务时自动下载）"""
    # 上一次没退干净 / 另一个实例还在翻译时，uv 覆盖引擎目录会撞文件锁，
    # 用户看到的只是一句权限错误。先讲清楚，别让人白等。
    # ★ 2026-10-21：没人管的残留**我们自己收掉**（客户反馈「任务管理器里根本杀不掉」），
    #   只有另一个还开着的星译在用的才留着。
    busy = busy_processes()
    r: dict = {}
    if busy:
        r = kill_leftovers(report, stage="engine")
        busy = busy_processes()
    if busy:
        if r.get("skipped") and not r.get("remaining"):
            raise EngineError(
                "另一个星译窗口正在使用翻译引擎（" + "、".join(busy) + "）。"
                "等它跑完，或先关掉那个窗口，再重新安装。")
        raise EngineError(
            "还有翻译引擎进程结束不掉（" + "、".join(busy) + "）。"
            "它多半卡在驱动或下载上，请重启电脑后再重新安装。")
    uv = install_uv(params, job, report)

    # 顺手把引擎数据目录挑好：有些机器上系统默认目录建不了目录链接（os error 448），
    # 引擎第一次跑任务就会卡在这儿。趁装引擎的时候先试出来，用户什么都不用管。
    datadir.ensure(params, report, stage="engine")

    # ★★ 空间不够就**当场停手**，别开始下（见 check_space 的说明）
    check_space(params, report)

    # 接下来要连着下几个 GB —— 别让系统睡过去把网断了
    _keep_awake(True)
    if not has_nvidia() and report:
        report("engine", None,
               "（没检测到 NVIDIA 显卡：引擎会用 CPU 跑，速度会明显慢 —— 但能用，不影响装）")

    report("engine", None, "正在安装翻译引擎…")
    # ★ 钉住 0.6.x：我们的 pylock / 路由配置 / 知识库都按 0.6 写死，
    #   上游一旦发 0.7 而不改这里，全新安装的机器会拿到一个没测过的引擎。
    #   0.6.x 的补丁版仍然能装上（上游修 bug 不受影响）。
    # 引擎的运行时 pylock 是 win-py312，工具环境也钉 3.12，避免 uv 挑到 3.11
    # 然后找不到随包的 cp312 wheel（2026-10-19 离线安装实测踩到）。
    base = [uv, "tool", "install", ENGINE_REQ, "--force", "--python", "3.12"]
    # 随包分发引擎本体和它的 Python 依赖（约 32MB）：先试本地 wheelhouse，
    # 再做网络兜底。这样没有代理、镜像又抽风的机器也能把引擎装上。
    local_whl = tools.bundle_dir() / "engine_whl"
    has_local = local_whl.is_dir() and any(local_whl.glob("finesub-*.whl"))
    # 直连上游源容易超时：先走清华镜像，失败再退回官方源。
    # 走不走镜像看的是那个地址**真的通不通**，不是「填没填」。
    # ⚠️ 有代理时也把清华源垫在官方源后面：用户填的代理可能只是端口开着、
    #    实际出不去（2026-10-19 全面检查时补的），官方源失败后还能靠镜像装上。
    tuna = not mirrors.effective_proxy(params)
    if tuna:
        net_tries = [base + ["--index-url", TUNA_INDEX], base]
    else:
        net_tries = [base, base + ["--index-url", TUNA_INDEX]]
    tries = ([base + ["--no-index", "--find-links", str(local_whl)]] if has_local else []) + net_tries
    if has_local:
        report("engine", None, "使用随包的翻译引擎组件…")
    out = ""
    r = None
    for i, cmd in enumerate(tries):
        if i:
            report("engine", None, "换一个下载源重试…")
        r = job.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env=_proxy_env(params))
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode == 0:
            break
    for line in out.splitlines()[-8:]:
        report("engine", None, line)
    if r is None or r.returncode != 0:
        raise RuntimeError(f"finesub 安装失败: {out[-400:]}")

    fb = find_finesub()
    if not fb:
        raise RuntimeError("安装完成但没找到引擎，请把日志发给作者")

    # 引擎的运行时依赖里有一个包只放在 GitHub 上（uv 按 pylock 里的地址直连，
    # 没有代理的机器必然失败）。这里把它改成本机分发，装运行时时就不用出网了。
    try:
        wheels.prepare(params, report)
    except Exception as e:  # noqa: BLE001 - 兜底机制失灵不该让「装引擎」这一步失败
        report("engine", None, f"（GitHub 依赖预置跳过：{e}）")

    # 引擎接下来要下的 6 个文件在 GitHub 上（uv / ffmpeg / MinGit / tokcount /
    # 人声分离模型 + 配置，共约 890MB）。2026-10-17 起直接用 GitHub 原始地址；
    # 老版本把清单改到过 COS 的，engine_mirror 会在这里恢复。
    # 改不动不算失败：那只是「回到慢」，不是「装不上」。
    try:
        engine_mirror.apply(report)
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（下载源恢复跳过：{e}）")

    # ★ ffmpeg 的 sha256 以前要问 api.github.com（60 次/小时/出口 IP，国内共享出口
    #   很容易被打满）。把 Date 构建钉死进清单，安装时就不再调 API。见 runtime_pin.py。
    try:
        import runtime_pin
        runtime_pin.apply(params, report)
    except Exception as e:  # noqa: BLE001 - 钉不上就退回 API，不拦安装
        report("engine", None, f"（ffmpeg 钉死跳过：{e}）")

    runtime_ok = _provision_runtime(params, job, report, fb)

    # 模型权重（人声分离 / 语音识别 / 第二模型校验，约 3.7GB）也趁这一步下齐。
    # 引擎自己是「用到才下」而且**那一步全程静默**（提示走 debug、进度被子进程吞掉、
    # 失败也吞掉）—— 用户会在第一次翻译时干等几分钟、什么都看不见（客户实测反复关软件）。
    # 用户点「一键安装引擎」是自愿的、进度就摆在眼前，这时候下最合适（2026-10-16 用户定）。
    # 失败不算失败：下不动就跳过，引擎用到时还会自己再试一次（见 modelfetch.py）。
    prefetched: dict = {}
    try:
        import modelfetch
        prefetched = modelfetch.prefetch_all(params, job, report)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（模型权重预取跳过：{e}）")

    # 内置术语词条：给用户的词条库打底（UE / Blender / Houdini / GPT …）。
    # 离线、幂等（内容没变时引擎自己报 "no change"）；失败不算失败——只是这次没导上，
    # 下次启动还会再试（见 builtin_kb.py，以及 winui_backend 的启动兜底）。
    #
    # ★ 运行环境没装好时**绝不能导**（2026-10-23 客户报障）：18 条词条 × 2 条命令
    #   会各起一个 finesub 进程，每个都重新尝试装 ffmpeg、各打一次 GitHub API。
    #   未认证配额是 60 次/小时按出口 IP 计，一轮就能烧光，下一轮 setup 必然限流 ——
    #   用户看到的就是「运行环境一直下不下来」。这里跳过，等环境好了下次启动自动补。
    if runtime_ok:
        try:
            import builtin_kb
            builtin_kb.ensure(params, job, report)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            report("engine", None, f"（内置词条导入跳过：{e}）")
    else:
        report("engine", None,
               "（运行环境还没准备好，先跳过内置词条导入——否则每条命令都会重试下载，反而更慢）")

    _keep_awake(False)                 # 装完了，恢复系统的睡眠策略
    if runtime_ok:
        report("engine", 100, "✅ 翻译引擎已就绪")
    else:
        # 别谎报成功：HomePage 会按 engine_status 判「能不能进翻译页」，
        # 这里把话说清楚，省得用户以为装好了、一翻译又失败。
        report("engine", 100,
               "⚠ 翻译引擎本体已装好，但运行环境没准备好 —— 请稍后点「重新试一次」。"
               "如果日志里出现「GitHub API 速率限制」，等约 1 小时再试，不要反复点。")
    if prefetched and all(prefetched.values()):
        report("engine", None, "模型权重已一并下好，第一次翻译不用再等下载")
    else:
        # 别把话说满：没下齐的那几样，引擎开跑时还会自己再试一次（那会儿进度很少）
        report("engine", None, "提示：还有模型没下齐，第一次翻译时会自动补上"
                               "（那几步界面上进度不多，请稍等，别关软件）")
    return fb


def _stream(job: Job, cmd: list[str], env: dict, report, stage: str = "engine") -> int:
    """跑一个命令并把输出**逐行**报给界面，返回退出码。

    长任务必须流式：uv 装 torch 要下载 2.6GB、好几分钟，`job.run(capture_output=True)`
    会把这几分钟的输出全憋到结束才吐出来——界面上就是一行不动，用户以为死机。
    """
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding="utf-8", errors="replace", env=env)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            report(stage, None, line)
    return p.wait()


def _provision_runtime(params: dict, job: Job, report, finesub: str) -> bool:
    """把引擎的 Python 运行环境先装好（`finesub setup`）

    **为什么要在这一步做**：运行时 = Python 环境 + CUDA 版 torch，解包约 2.8GB、
    下载 2.6GB。以前这块留到「第一次点开始翻译」时才下，而那几分钟里界面上只有一行
    「正在安装 FineSub AI 依赖」，用户既看不到进度也不知道要等多久 ——
    2026-10-08 的客户就是在这儿反复关掉软件重开，留下一堆没退干净的引擎进程互相抢
    文件锁，把后面的失败堆成了一串。

    引擎自带 `finesub setup`（provision the runtime without running），趁「一键安装引擎」
    这一步跑掉最合适：用户是自愿点这个按钮的，下载大也认，而且进度就摆在眼前。

    **失败不算失败**：真出问题的话第一次翻译还会再试一次，那时的报错更具体；
    这里只提示一句，不把「装引擎」这一步判死。

    返回 True / False：调用方据此决定要不要继续做**依赖运行环境**的事
    （目前是内置词条导入）；False 时那些事一律跳过，避免反复触发下载。
    """
    report("engine", None, "正在准备引擎运行环境（首次要下载 2~3 GB，请保持网络畅通、别关软件）…")
    env = _proxy_env(params)
    if params.get("finesub_home"):
        env["FINESUB_HOME"] = params["finesub_home"]
    env["FINESUB_HF_ENDPOINT"] = params.get("hf_endpoint") or "https://hf-mirror.com"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        code = _stream(job, [finesub, "setup"], env, report)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（运行环境预置跳过：{e}）")
        return False
    if code == 0:
        report("engine", None, "✅ 引擎运行环境已就绪")
        return True
    report("engine", None,
           f"⚠ 运行环境这次没准备好（退出码 {code}）——第一次翻译时会自动再试一次。"
           f"如果一直失败，请把诊断报告发给我们。")
    return False
