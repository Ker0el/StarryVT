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
import re
import shutil
import subprocess
import tempfile
import time
import threading
import zipfile
from pathlib import Path

from job import Cancelled, Job, no_window_kwargs
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

#: 引擎 CLI 的串行闸。内置词条导入（一次性几十次 `finesub knowledge …`）不能和
#: 「装引擎」「任务开工前的残留清理」交叉 —— 否则开工前那次 kill 会把**我们自己**
#: 刚 spawn 的词条进程当成残留杀掉，紧接着 busy_processes 又看到新进程，
#: 用户得到的是「后台还有没退干净的引擎进程，请重启电脑」（2026-10-25 客户实测）。
CLI_LOCK = threading.RLock()
#: 有翻译任务准备开工时置位；词条导入看到它就在下一条之前收手（见 builtin_kb.ensure）。
TASK_WAITING = threading.Event()


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
    """找 finesub 引擎：设置里的自定义路径 > PATH > 常见位置

    ★ 只认**文件**（2026-10-25 客户报障）：以前只要 `exists()` 就返回，
      客户把「翻译引擎路径」填成了程序目录 `D:\\StarryVT`，于是 Popen(目录)
      抛 `PermissionError: [WinError 5]` —— 界面只有一句「拒绝访问」，
      翻译和启动时的词条导入全都失败，且看不出是设置填错了。
      现在填目录时先看目录里有没有 finesub.exe；没有就当作没填，继续往下找。
    """
    hint = (exe_hint or "").strip().strip('"').strip()
    if hint:
        p = Path(hint)
        if p.is_file():
            return str(p)
        if p.is_dir():
            inside = p / "finesub.exe"
            if inside.is_file():
                return str(inside)
    p = shutil.which(hint or "finesub")
    if p and Path(p).is_file():
        return p
    # uv 允许把工具的可执行文件放到自定义目录（UV_TOOL_BIN_DIR）；用户设了它、
    # 又没加进 PATH 时，刚装好的引擎会找不到。先看这里，再看两个默认位置。
    tool_bin = os.environ.get("UV_TOOL_BIN_DIR", "").strip()
    if tool_bin:
        c = Path(tool_bin) / "finesub.exe"
        if c.is_file():
            return str(c)
    for c in (Path.home() / ".local" / "bin" / "finesub.exe",
              BIN_DIR / "finesub.exe"):
        if c.is_file():
            return str(c)
    return None


_HOME_IN_VENV = re.compile(r"(?im)^\s*home\s*=\s*(.+?)\s*$")
#: 启动器里内嵌的解释器路径（uv/pip 的 Windows 启动器把 shebang 塞在 zip 尾部）
_SHEBANG_PY = re.compile(rb"([A-Za-z]:\\[^\x00\r\n\"']{1,300}?\\Scripts\\python\.exe)", re.I)
#: 启动器 → 工具环境根目录 的解析结果（只缓存 47KB 二进制扫描这一步；
#: 解释器在不在每次都重新 stat —— 用户可能刚装好 Python，不能一直拿旧结论）
_VENV_ROOT_CACHE: dict[tuple[str, float, int], Path | None] = {}


def _venv_root_of_launcher(exe: Path) -> Path | None:
    """`finesub.exe` 对应的 Python 工具环境根目录（找不到返回 None）

    两种形态都要认：
      · `%APPDATA%\\uv\\tools\\finesub\\Scripts\\finesub.exe` —— 上级就是工具环境；
      · `%USERPROFILE%\\.local\\bin\\finesub.exe` —— 只是启动器的**副本**，
        真正的工具环境写在它内嵌的 shebang 里（`…\\Scripts\\python.exe`）。
    """
    direct = exe.parent.parent / "pyvenv.cfg"
    if direct.is_file():
        return exe.parent.parent
    try:
        blob = exe.read_bytes()
    except OSError:
        return None
    m = _SHEBANG_PY.search(blob)
    if not m:
        return None
    try:
        py = Path(os.fsdecode(m.group(1)))
    except Exception:  # noqa: BLE001
        return None
    if py.name.lower() != "python.exe" or py.parent.name.lower() != "scripts":
        return None
    root = py.parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def shim_problem(exe: str) -> str:
    """这个 finesub.exe 背后的 Python 还在不在？正常返回 ""，坏了返回人话。

    ★ 2026-10-26 客户报障（Win11 / AMD，装了两天一次没翻成）：
      uv 生成的 `finesub.exe` 只是个启动器，真正干活的是工具环境里的
      `Scripts\\python.exe`；而那个 python 又按 `pyvenv.cfg` 的 `home =` 去找
      uv 托管目录里的解释器。托管目录被清理工具删掉、或者变成「不受信任的装入点」
      （os error 448）时，启动器只吐一行 `No Python at '…'`，退出码 103 ——
      界面却按「没有堆栈 = 硬死」提示「内存不够 / 杀毒软件 / 显卡驱动」，
      客户照着试一整天也好不了。

    这里**不启动引擎**（启动它可能触发下载），只顺着 pyvenv.cfg 查文件在不在。
    拿不准（不是 venv 启动器 / 读不了）一律当正常，避免误拦能用的机器。
    """
    try:
        p = Path(str(exe))
        if p.suffix.lower() != ".exe":
            return ""
        st = p.stat()
        key = (str(p).lower(), st.st_mtime, st.st_size)
        if key in _VENV_ROOT_CACHE:
            root = _VENV_ROOT_CACHE[key]
        else:
            root = _venv_root_of_launcher(p)
            _VENV_ROOT_CACHE[key] = root
        if root is None:
            return ""
        py = root / "Scripts" / "python.exe"
        if not py.is_file():
            return f"翻译引擎的 Python 启动器不完整：找不到 {py}"
        try:
            text = (root / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"翻译引擎的 Python 环境读不了（{e.__class__.__name__}: {e}）"
        m = _HOME_IN_VENV.search(text)
        if not m:
            return ""
        home = m.group(1).strip().strip('"').strip("'")
        if home:
            base_py = Path(home) / "python.exe"
            try:
                ok = base_py.is_file()
            except OSError:  # 不受信任的装入点可能直接抛
                ok = False
            if not ok:
                return (f"翻译引擎的 Python 解释器不见了：{base_py}\n"
                        "（运行库或清理软件删掉了 uv 的托管 Python，或者那个目录成了"
                        "不可访问的目录链接）")
        return ""
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 - 检查本身不该影响任何正常流程
        return ""


def status(exe_hint: str = "") -> dict:
    """引擎状态：{ready, engine_path, engine_problem, uv_path, uv_ready, ...}

    ★ `ready` 必须是「本体 + 运行环境都在」（2026-10-23 客户报障）：
      以前只看 finesub.exe 在不在，于是 `finesub setup` 失败（常见是 GitHub API
      限流导致 ffmpeg 装不上）后，首页照样打勾、按钮变成「进入本地翻译」，
      用户一进去就撞「引擎没装好」。运行环境才是能不能翻译的分界线。
    ★ 2026-10-26 再补一条：启动器背后的 Python 也算「本体」。文件都在、但
      解释器被删的机器，以前首页照样显示「已就绪」，一点翻译就失败。
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
    problem = shim_problem(fb) if fb else ""
    ok = bool(fb) and runtime_ok and not problem
    return {
        "ready": ok,
        "ok": ok,
        "engine_path": fb or "",
        "engine_problem": problem,
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


def _uv_python_dir(params: dict) -> Path:
    """uv 的托管 Python 目录 —— `<数据目录>\\runtime\\uv-python`（我们自己的）

    ★ 为什么不能让 uv 用默认的 `%APPDATA%\\uv\\python`（2026-10-25 客户报障）：
      默认目录里会留下**别的权限级别**建的「小版本别名」junction，uv 在解析
      `--python 3.12` 时会逐个检查那里的安装，遇到不信任的装入点就**整体失败**
      （os error 448），连 `uv tool install` 都进不去。这是 uv 0.11/0.13 的行为，
      上游还没修（astral-sh/uv#19220）。引擎自己的 `finesub setup` 一直设着
      UV_PYTHON_INSTALL_DIR，所以只有外层装引擎这一步会踩到。

    ⚠️ 不复用引擎的 `runtime\\python-builds`：两边不一定跑同一版 uv
      （用户 PATH 上那份可能是老的），别让旧 uv 去动引擎运行时共用的目录。
      多下的这份 3.12 只有 20 多 MB。
    """
    home = (params.get("finesub_home") or "").strip()
    base = Path(home) if home else datadir.default_home()
    base = base / "runtime" / "uv-python"
    if not str(base).isascii():
        # ★ 2026-11-05 客户报告（引擎数据目录 `E:\\星译\\StarryVT\\data\\engine`）：
        #   克隆环境的 `.venv\\Scripts\\python.exe` 启动器按系统代码页解析路径，
        #   `星译` 变成 `??`，只吐一行
        #   `No Python at '"E:\\??\\StarryVT\\...\\python.exe'`、退出码 103，
        #   209 句一句都没合成。托管解释器（以及用它建的 venv）一律不放中文路径。
        base = _ascii_uv_python_dir()
    return base


def _ascii_uv_python_dir() -> Path:
    """托管 Python 的纯英文落脚点（连用户名都可能是中文，所以逐个候选验一遍）"""
    cands = []
    for root in (os.environ.get("LOCALAPPDATA"),
                 os.environ.get("PUBLIC"),
                 os.environ.get("PROGRAMDATA")):
        if root and Path(root).is_absolute():
            cands.append(Path(root) / "StarryVT" / "uv-python")
    for c in cands:
        if str(c).isascii():
            return c
    return Path(r"C:\ProgramData\StarryVT\uv-python")


def uv_env(params: dict) -> dict:
    """给 uv 子进程用的环境：代理 + GitHub 加速前缀 + Python 下载镜像 + UTF-8 输出。

    本地语音克隆（localclone.py）也走 uv，别再各自拼一份 —— 2026-10-19 那边
    调了一个不存在的 `_uv_env`，把「下载本地语音克隆」整个卡死在第一步。
    """
    env = _proxy_env(params)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 见 _uv_python_dir：不能让它去扫 %APPDATA%\uv\python 里的坏 junction
    env["UV_PYTHON_INSTALL_DIR"] = str(_uv_python_dir(params))
    return env


#: 「引擎的 Python 环境坏了」在输出里的痕迹（和「下载失败」是两回事）
_BROKEN_ENV_MARKERS = (
    "dll load failed",
    "_pydantic_core",
    "importerror:",
    "modulenotfounderror",
    "no python at",
)


def env_broken(lines) -> bool:
    """`finesub …` 的输出是不是「环境坏了」而不是「网络不好」

    ★ 2026-11-05 客户报告：`ImportError: DLL load failed while importing
      _pydantic_core: 找不到指定的程序`（WinError 127）被当成「下载被限流」，
      自动换了 2 次下载源、界面劝客户「等约 1 小时」，两天点了 67 次。
      127 是「依赖 DLL 里没有它要的导出」—— 下载、重试、换镜像都不可能修好它。
    """
    if isinstance(lines, (list, tuple)):
        text = "\n".join(str(x) for x in lines)
    else:
        text = str(lines or "")
    low = text.lower()
    return any(m in low for m in _BROKEN_ENV_MARKERS)


def _tool_dir() -> Path:
    """uv 的工具环境根目录（`UV_TOOL_DIR` 优先，默认 `%APPDATA%\\uv\\tools`）"""
    custom = os.environ.get("UV_TOOL_DIR", "").strip()
    if custom:
        return Path(custom)
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "uv" / "tools"


def tool_python(exe: str = "") -> Path | None:
    """引擎工具环境里的 `Scripts\\python.exe`（没有就返回 None）"""
    root: Path | None = None
    fb = exe or find_finesub()
    if fb:
        try:
            root = _venv_root_of_launcher(Path(fb))
        except OSError:
            root = None
    if root is None:
        cand = _tool_dir() / "finesub"
        root = cand if (cand / "pyvenv.cfg").is_file() else None
    if root is None:
        return None
    py = root / "Scripts" / "python.exe"
    return py if py.is_file() else None


def env_probe(exe: str = "") -> dict:
    """引擎解释器是谁、编译扩展能不能导入

    返回 `{python, version, base_prefix, import_ok, error}`。
    ★ 2026-11-05 起：诊断报告和失败日志都带这一份。上次客户报告里
      `DLL load failed … 找不到指定的程序` 查不出用的是哪个 Python，
      只能让客户再点一次。
    """
    out = {"python": "", "version": "", "base_prefix": "", "import_ok": None, "error": ""}
    py = tool_python(exe)
    if py is None:
        return out
    out["python"] = str(py)
    code = ("import sys\n"
            "print(sys.version.split()[0])\n"
            "print(sys.base_prefix)\n"
            "import pydantic_core\n"
            "print('ok', pydantic_core.__version__)\n")
    try:
        # cwd 用工具环境根目录：别让用户当前目录里的同名 .pyd / .dll 抢先
        r = subprocess.run([str(py), "-c", code],
                           **no_window_kwargs(capture_output=True, text=True,
                                              encoding="utf-8", errors="replace",
                                              timeout=30, cwd=str(py.parent.parent)))
    except Exception as e:  # noqa: BLE001 - 探针失败本身不能影响任何流程
        out["error"] = f"{e.__class__.__name__}: {e}"
        return out
    parts = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if parts:
        out["version"] = parts[0]
    if len(parts) > 1:
        out["base_prefix"] = parts[1]
    out["import_ok"] = r.returncode == 0 and any(p.startswith("ok ") for p in parts)
    if not out["import_ok"]:
        err = [ln for ln in (r.stderr or "").splitlines() if ln.strip()]
        out["error"] = (err[-1] if err else f"退出码 {r.returncode}")[:300]
    return out


# ---------- 引擎用哪个 Python ----------

#: 最近一次「装运行环境」失败的人话原因（界面上的错误框直接用它）
_LAST_SETUP_REASON = ""
_REASON_LOCK = threading.Lock()


def last_setup_reason() -> str:
    with _REASON_LOCK:
        return _LAST_SETUP_REASON


def _set_setup_reason(text: str) -> None:
    global _LAST_SETUP_REASON
    with _REASON_LOCK:
        _LAST_SETUP_REASON = text


#: 「系统里那份 Python 是 32 位」在引擎输出里的痕迹
#: ★ 2026-11-05 客户报障：机器上有一个 Python 3.12.0a2 **32 位**（Python312-32），
#:   引擎的 bootstrap 见到「系统 Python 3.12」就直接复用，建出来的 venv 也是 32 位，
#:   而依赖里 onnxruntime 只有 win_amd64 轮子 —— uv 当场报
#:   `Package onnxruntime can't be installed because it doesn't have a ... wheel
#:   for the current platform`，环境永远装不完。
_BAD_PYTHON_MARKERS = (
    "doesn't have a source distribution or wheel for the current platform",
    "only has wheels for the following platform",
    "platform_machine == 'amd64'",
)

#: 「网络不通 / 节点太慢」在引擎输出里的痕迹（和「环境坏了」「服务器 404」分开）
_NETWORK_MARKERS = (
    "timed out", "timeout", "handshake", "certificate verify failed",
    "urlopen error", "econnreset", "econnrefused", "econnaborted",
    "connection reset", "connection aborted", "failed to fetch",
    "network is unreachable", "no route to host",
    "temporary failure in name resolution", "name or service not known",
    "10053", "10054", "10060", "10061",
    "远程主机强迫关闭", "目标计算机积极拒绝", "连接尝试失败", "连接超时",
)


def _tail_text(lines) -> str:
    if isinstance(lines, (list, tuple)):
        return "\n".join(str(x) for x in lines)
    return str(lines or "")


def bad_system_python(lines) -> bool:
    """引擎的失败是不是「它复用了系统里的 32 位 Python」"""
    low = _tail_text(lines).lower()
    return any(m in low for m in _BAD_PYTHON_MARKERS)


def network_broken(lines) -> bool:
    """引擎的失败是不是网络（连不上 / 节点太慢），而不是环境本身坏了"""
    low = _tail_text(lines).lower()
    return any(m in low for m in _NETWORK_MARKERS)


#: 问一个解释器：几位 + 版本（`py -3.12` 这类要带额外参数）
_BITS_CODE = ("import struct,sys\n"
              "print(struct.calcsize('P') * 8)\n"
              "print('%d.%d' % sys.version_info[:2])\n")
_PY_BITS_CACHE: dict[str, tuple[int, str]] = {}
_PY_BITS_LOCK = threading.Lock()


def _probe_python(cmd: list[str]) -> tuple[int, str]:
    """(位数, "3.12")；跑不起来返回 (0, "")"""
    key = "\0".join(cmd)
    with _PY_BITS_LOCK:
        hit = _PY_BITS_CACHE.get(key)
    if hit is not None:
        return hit
    bits, ver = 0, ""
    try:
        r = subprocess.run(
            cmd,
            **no_window_kwargs(capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=8))
        lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if r.returncode == 0 and len(lines) >= 2:
            bits, ver = int(lines[0]), lines[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    with _PY_BITS_LOCK:
        _PY_BITS_CACHE[key] = (bits, ver)
    return bits, ver


def _python_bits(exe, args=()) -> tuple[int, str]:
    return _probe_python([str(exe), *args, "-c", _BITS_CODE])


def _engine_python(params: dict) -> Path | None:
    """一份 **64 位** 的 Python 3.12（引擎建运行时环境时用它，不用系统里那份）

    优先级：uv 托管的那份 → 引擎工具环境的解释器。两者都没有就返回 None，
    让引擎自己按 UV_PYTHON_INSTALL_DIR 去下一份 64 位的。
    """
    # 先用 uv 托管的那份（是**真正的解释器**，不是 venv 启动器）：
    # `uv venv --python <venv 里的 python.exe>` 要多绕一层，能避则避。
    base = _uv_python_dir(params)
    try:
        cands = sorted(base.glob("cpython-3.12*windows-x86_64*/python.exe"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        cands = []
    for c in cands:
        if c.is_file() and _python_bits(c)[0] == 64:
            return c
    py = tool_python()
    if py is not None and _python_bits(py)[0] == 64:
        return py
    return None


def _py_launcher_32() -> str:
    """系统 `py` 启动器解析出来的 3.12 是 32 位时，返回启动器路径；否则返回 "" """
    launcher = shutil.which("py")
    if not launcher:
        return ""
    bits, _ = _probe_python([launcher, "-3.12", "-c", _BITS_CODE])
    return launcher if bits == 32 else ""


def _py_shim_source() -> Path | None:
    r"""venv 里的启动器（uv 工具环境的 `Scripts\python.exe`）"""
    py = tool_python()
    if py is None:
        return None
    try:
        if (py.parent.parent / "pyvenv.cfg").is_file():
            return py
    except OSError:
        pass
    return None


def _py_shim_dir(source: Path | None) -> Path | None:
    """放一个「跑不起来的 py.exe」，把系统的 py 启动器挡在后面

    引擎的 `_find_system_python()` 先试 `py -3.12`、再试 PATH 上的 python。PATH
    我们能改，py 启动器走注册表，改不了；但它用的 `shutil.which("py")` 认 PATH
    顺序。venv 里的 `python.exe` 是个启动器，拷到没有 pyvenv.cfg 的目录里只会打印
    `No pyvenv.cfg file` 并退出（实测 106），正好让引擎跳过这个候选。
    """
    if source is None or not source.is_file():
        return None
    # 只能从 venv 拿启动器（Scripts\python.exe 旁边有 pyvenv.cfg）：拷一份真的
    # python.exe 出去没有 python312.dll，Windows 会弹「找不到 DLL」的对话框。
    try:
        if not (source.parent.parent / "pyvenv.cfg").is_file():
            return None
    except OSError:
        return None
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    shim = root / "StarryVT" / "py-shim"
    dst = shim / "py.exe"
    try:
        if not dst.is_file() or dst.stat().st_size != source.stat().st_size:
            shim.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dst)
    except OSError:
        return None
    return shim


_PLAN_CACHE: dict[str, dict] = {}
_PLAN_LOCK = threading.Lock()


def engine_python_plan(params: dict) -> dict:
    """引擎子进程用哪个 Python、要挡掉哪些路径

    ★ 2026-11-05 客户报障：系统里装的是 **32 位** 的 Python 3.12.0a2，
      引擎直接复用 → 依赖（onnxruntime）没有 32 位轮子 → 运行环境永远装不好。
      这里让引擎只看得见 64 位的那份：
        · PATH 上 32 位的 Python 3.12 目录整条删掉；
        · 系统 `py` 启动器指向 32 位时，用一个假 py.exe 把它挡掉；
        · 把我们自己那份 64 位解释器放在 PATH 最前面。
    返回 `{python, blocked, shim, path}`。
    """
    good = _engine_python(params)
    launcher = _py_launcher_32()
    env_path = os.environ.get("PATH", "")
    key = "\0".join([os.path.normcase(str(good or "")), launcher,
                     os.path.normcase(env_path), str(params.get("finesub_home") or "")])
    with _PLAN_LOCK:
        hit = _PLAN_CACHE.get(key)
    if hit is not None:
        return hit

    blocked: list[str] = []
    keep: list[str] = []
    seen: set[str] = set()
    for entry in env_path.split(os.pathsep):
        if not entry:
            continue
        norm = os.path.normcase(entry)
        if norm in seen:
            continue
        seen.add(norm)
        is32 = False
        for name in ("python.exe", "python3.12.exe", "python3.exe"):
            cand = Path(entry) / name
            try:
                if cand.is_file() and _python_bits(cand) == (32, "3.12"):
                    is32 = True
                    break
            except OSError:
                continue
        (blocked if is32 else keep).append(entry)

    parts: list[str] = []
    shimmed = False
    if launcher:
        # py.exe 不在 Windows 目录里时，直接把它那条 PATH 去掉最省事。
        windir = os.path.normcase(os.environ.get("WINDIR") or "C:\\Windows")
        launcher_dir = os.path.normcase(str(Path(launcher).parent))
        if not launcher_dir.startswith(windir):
            keep = [e for e in keep if os.path.normcase(e) != launcher_dir]
            if launcher_dir not in {os.path.normcase(b) for b in blocked}:
                blocked.append(str(Path(launcher).parent))
        else:
            # 装在 Windows 目录里的 py.exe 不能从 PATH 里拿掉（系统命令还要用），
            # 只能用一个跑不起来的 py.exe 抢在它前面。
            shim = _py_shim_dir(_py_shim_source())
            if shim is not None:
                parts.append(str(shim))
                shimmed = True
    if good is not None:
        parts.append(str(Path(good).parent))
    parts.extend(keep)
    plan = {
        "python": str(good) if good is not None else "",
        "blocked": blocked,
        "shim": shimmed,
        "path": os.pathsep.join(parts),
    }
    with _PLAN_LOCK:
        _PLAN_CACHE[key] = plan
    return plan


def engine_child_path(params: dict) -> str:
    """给 `finesub` 子进程用的 PATH（见 engine_python_plan）"""
    return engine_python_plan(params)["path"]


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
    # 随包优先（2026-11-05）：bundle_runtime\uv.zip 是打包时预下载的同一份 uv。
    # 没代理的新用户走到这里是安装流程的第一步，能不出网就不出网。
    bundled_uv = tools.bundle_dir() / "bundle_runtime" / "uv.zip"
    if bundled_uv.is_file() and bundled_uv.stat().st_size > 0:
        shutil.copy2(bundled_uv, zip_path)
        report("engine", None, "使用随包的 uv")
    else:
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
    # ★ 和内置词条导入串起来（2026-10-25/26 客户报障）：启动时的词条导入会在后台
    #   连续 spawn `finesub.exe`，下面那次 kill_leftovers 会把它们当残留杀掉，
    #   紧接着 busy_processes() 又看到新进程 → 安装/开工被无故拦住。
    #   用 RLock：装完引擎后同一个线程还会调 builtin_kb.ensure（末尾）。
    with CLI_LOCK:
        return _install_finesub(params, job, report)


def _run_tool_install(params: dict, job: Job, report, uv: str, *,
                      python: str = "3.12", reinstall: bool = False) -> str:
    """`uv tool install finesub`：随包 wheelhouse 优先，再走镜像/官方源

    ★ 钉住 0.6.x：我们的 pylock / 路由配置 / 知识库都按 0.6 写死，上游一旦发 0.7
      而不改这里，全新安装的机器会拿到一个没测过的引擎。0.6.x 的补丁版仍能装上。
    ★ 引擎的运行时 pylock 是 win-py312，工具环境也钉 3.12，避免 uv 挑到 3.11
      然后找不到随包的 cp312 wheel（2026-10-19 离线安装实测踩到）。
    `python` 可以是一份**托管解释器的绝对路径**（见 _managed_python）：这样 uv 不会
    回落到用户 PATH 上那份来路不明的 Python（2026-11-05 客户报障）。
    """
    base = [uv, "tool", "install", ENGINE_REQ, "--force", "--python", python]
    if reinstall:
        base.append("--reinstall")
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
    failed: list[str] = []
    env = uv_env(params)          # ★ 必须用 uv_env：里面带着 UV_PYTHON_INSTALL_DIR
    for i, cmd in enumerate(tries):
        if i:
            report("engine", None, "换一个下载源重试…")
        r = job.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    env=env)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode == 0:
            break
        # ★ 每一次失败都留一行（2026-11-05 客户一）：以前只回显**最后一次**的输出，
        #   于是「随包的组件为什么装不上、才退回网上拿了个没测过的 pydantic」永远查不到。
        why = [ln.strip() for ln in out.splitlines() if ln.strip()][-2:]
        if why:
            failed.append(f"第 {i + 1} 个源失败：" + " / ".join(why)[:300])
    for line in out.splitlines()[-8:]:
        report("engine", None, line)
    if failed and (r is None or r.returncode != 0):
        for line in failed:
            report("engine", None, line)
    if r is None or r.returncode != 0:
        if datadir.looks_blocked(out):
            # 2026-10-25 客户报障：uv 不信 %APPDATA%\uv\python 里的老 junction，
            # 整个发现过程直接失败。人话讲清楚，别把 uv 的英文原文丢出去。
            where = env.get("UV_PYTHON_INSTALL_DIR", "")
            raise RuntimeError(
                "Windows 拒绝访问引擎的 Python 目录（os error 448 / 不受信任的装入点）。\n"
                f"目录：{where}\n"
                "请把该目录加入杀毒软件白名单；如果星译是「以管理员身份运行」启动的，"
                "改成普通方式启动（或反过来）后，再点一次「一键安装引擎」。")
        raise RuntimeError(f"finesub 安装失败: {out[-400:]}")
    return out


def _managed_python(params: dict, job: Job, report) -> str:
    """让 uv 装一份**托管**的 3.12 并返回它的绝对路径（失败返回 ""）

    ★ 2026-11-05 客户报障：`--python 3.12` 在托管目录里没有解释器时，uv 会
      **回落到 PATH 上的系统 Python**（本机复现过）。客户机器上那份解释器加载
      `_pydantic_core` 直接 `ERROR_PROC_NOT_FOUND`（找不到指定的程序），
      `finesub setup` 每次退出码 1，界面却报「下载被限流」。
      自己点名用托管解释器，环境就是所有机器都一样的那份。
    """
    uv = find_uv()
    if not uv:
        return ""
    env = uv_env(params)
    report("engine", None, "准备引擎专用的 Python（3.12）…")
    try:
        r = job.run([uv, "python", "install", "3.12"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（托管 Python 没装上：{e}）")
        return ""
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
        report("engine", None, "（托管 Python 没装上：" + " / ".join(tail) + "）")
        return ""
    d = _uv_python_dir(params)
    cands = []
    try:
        cands = sorted(d.glob("cpython-3.12.*-windows-*/python.exe"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        cands = []
    for c in cands:
        if c.is_file():
            return str(c)
    # uv 把解释器放在了别处（用户自己设了别的目录）时，问它要
    try:
        r = job.run([uv, "python", "find", "3.12"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env)
        line = ((r.stdout or "").strip().splitlines() or [""])[-1].strip()
        if r.returncode == 0 and line and Path(line).is_file():
            return line
    except Cancelled:
        raise
    except Exception:  # noqa: BLE001
        pass
    return ""


def repair_env(params: dict, job: Job, report) -> bool:
    """重建引擎的 Python 工具环境（导入阶段就崩时用），成功返回 True

    顺序：删工具环境 → 装托管 3.12 → 用它 `--reinstall` 重装引擎 → 看启动器背后的
    解释器还在不在。客户机器上「装好的东西重新装一遍」是最省事的自愈手段：
    uv 缓存里的 wheel 都还在，不用重新下载。
    """
    uv = find_uv()
    if not uv:
        report("engine", None, "（重建环境失败：找不到 uv）")
        return False
    report("engine", None, "重建引擎运行环境（删掉旧环境 → 用托管 Python 重装）…")
    root = _tool_dir() / "finesub"
    try:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    except OSError as e:
        report("engine", None, f"（删不掉旧工具环境：{e}）")
    py = _managed_python(params, job, report)
    try:
        _run_tool_install(params, job, report, uv,
                          python=py or "3.12", reinstall=True)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（重建环境失败：{e}）")
        return False
    # ★ 验的是**刚重建的这个环境**，不是 find_finesub() 随手挑到的那份：
    #   用户设置里可能还留着老路径（2026-11-05 实测：设了 UV_TOOL_BIN_DIR 时
    #   find_finesub 会挑到别处，重建成功也会被误判成失败）。
    py = root / "Scripts" / "python.exe"
    if not py.is_file():
        report("engine", None, f"（重建后没找到解释器：{py}）")
        return False
    probe = env_probe(str(root / "Scripts" / "finesub.exe"))
    if probe["python"]:
        report("engine", None,
               f"引擎解释器: {probe['python']}（{probe['version'] or '?'}）")
    if probe["import_ok"] is False:
        report("engine", None, f"（重建后导入仍然失败：{probe['error']}）")
        return False
    return True


def _install_finesub(params: dict, job: Job, report) -> str:
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
    _run_tool_install(params, job, report, uv)

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
               "⚠ 翻译引擎本体已装好，但运行环境没准备好 —— 原因见上面那一行；"
               "能自动修的（重建环境、换下载源）已经修过了。仍失败请点「环境自检」"
               "导出诊断报告发给我们。")
    if prefetched and all(prefetched.values()):
        report("engine", None, "模型权重已一并下好，第一次翻译不用再等下载")
    else:
        # 别把话说满：没下齐的那几样，引擎开跑时还会自己再试一次（那会儿进度很少）
        report("engine", None, "提示：还有模型没下齐，第一次翻译时会自动补上"
                               "（那几步界面上进度不多，请稍等，别关软件）")
    return fb


#: 引擎的下载缓存超过这个大小就先清一次（见 prune_engine_cache）
CACHE_PRUNE_GB = 6.0


def engine_cache_bytes(params: dict) -> int:
    """引擎的下载缓存现在占多少字节（`cache\\uv` + `cache\\downloads`）"""
    home = (params.get("finesub_home") or "").strip()
    base = Path(home) if home else datadir.default_home()
    total = 0
    for sub in ("uv", "downloads"):
        d = base / "cache" / sub
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def prune_engine_cache(params: dict, report=None, stage: str = "engine",
                       force: bool = False) -> int:
    """清掉**没有被运行环境引用**的缓存（`uv cache prune`），返回清完还剩多少字节

    为什么要有（2026-10-24 客户报障）：引擎只在**安装成功之后**才 prune。装失败或
    被取消时 staging 被删掉，缓存里的轮子却全都留着 —— 那台机器 `cache` 40.4 GB、
    模型才 3.5 GB，D 盘就被这一项吃红了。

    阈值（{CACHE_PRUNE_GB}）是刻意的：一次正常下载约 5 GB，低于阈值说明可能是
    网络抖动后重试，**不清**（清了就要重下 2.6 GB）；反复失败累积到阈值以上才清。
    已装好环境的机器清了也腾不出空间（缓存和运行环境是硬链接），但也不会删坏。
    """
    size = engine_cache_bytes(params)
    if not force and size < int(CACHE_PRUNE_GB * (1 << 30)):
        return size
    uv = find_uv()
    if not uv:
        return size
    home = (params.get("finesub_home") or "").strip()
    base = Path(home) if home else datadir.default_home()
    env = _proxy_env(params)
    try:
        r = subprocess.run(
            [uv, "cache", "prune", "--cache-dir", str(base / "cache" / "uv")],
            **no_window_kwargs(capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=300, env=env))
    except Exception as e:  # noqa: BLE001 - 清缓存失败绝不能拖累安装
        if report:
            report(stage, None, f"（清理引擎缓存跳过：{e}）")
        return size
    after = engine_cache_bytes(params)
    if report:
        if size > after:
            report(stage, None,
                   f"已清理引擎缓存 {size / 2**30:.1f} GB → {after / 2**30:.1f} GB"
                   f"（上次安装失败留下的，不影响已经装好的环境）")
        elif r.returncode != 0:
            report(stage, None, f"（清理引擎缓存失败，退出码 {r.returncode}，继续安装）")
    return after


def _stream(job: Job, cmd: list[str], env: dict, report, stage: str = "engine",
            tail: list[str] | None = None) -> int:
    """跑一个命令并把输出**逐行**报给界面，返回退出码。

    长任务必须流式：uv 装 torch 要下载 2.6GB、好几分钟，`job.run(capture_output=True)`
    会把这几分钟的输出全憋到结束才吐出来——界面上就是一行不动，用户以为死机。

    tail 不为 None 时顺便留最后几十行：失败后要据此认出「是哪一类失败」
    （例如 c10.dll 那句 WinError 1114 埋在 90 行包清单后面，光看退出码认不出）。
    """
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding="utf-8", errors="replace", env=env)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            report(stage, None, line)
            if tail is not None:
                tail.append(line)
                if len(tail) > 80:
                    del tail[0]
    return p.wait()


def _setup_env(params: dict) -> dict:
    """跑 `finesub` 子进程用的环境（代理 / 镜像 / 引擎数据目录 / 托管 Python 目录）

    放在一处：以前「首次」和「换条路重试」各拼一份，改一处漏一处。
    带上 UV_PYTHON_INSTALL_DIR 是为了让引擎自己建运行时 venv 时也用同一份托管
    Python（中文数据目录会被换到纯英文位置，见 _uv_python_dir）。
    """
    env = uv_env(params)
    if params.get("finesub_home"):
        env["FINESUB_HOME"] = params["finesub_home"]
    env["FINESUB_HF_ENDPOINT"] = params.get("hf_endpoint") or "https://hf-mirror.com"
    env["PYTHONIOENCODING"] = "utf-8"
    # ★ 2026-11-05：引擎的 bootstrap 会优先复用「系统里的 Python 3.12」，
    #   客户机上那份是 **32 位**（Python312-32），建出来的运行环境装不了
    #   onnxruntime（只有 win_amd64 轮子）。这里把 PATH 换掉，让它只看得见
    #   我们自己那份 64 位解释器（见 engine_python_plan）。
    try:
        env["PATH"] = engine_child_path(params)
    except Exception:  # noqa: BLE001 - 探测失败就退回原样，别拦住安装
        pass
    return env


def _dir_size(root: Path) -> int:
    """目录里所有普通文件的总字节数（走不通就返回 0，别让进度条把安装带崩）"""
    total = 0
    stack = [str(root)]
    try:
        while stack:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat().st_size
                    except OSError:
                        continue
    except OSError:
        pass
    return total


#: torch 的 wheel 在 lock 里没有 size（来自 download.pytorch.org），按经验补齐
_TORCH_BYTES = 2600 * 2**20


def _runtime_download_total() -> int:
    """引擎运行环境装完大概占多少字节（下载 + 解包）——进度条的分母

    分母读引擎自带的 pylock（每个 wheel 都写了 size）；torch 那条没写，用经验值。
    读不到就按 6 GB 估。估得不准没关系：进度条只要在动、字节数在涨，
    用户就知道没卡死（2026-11-05 用户反馈：初始化下载看不见进度）。
    """
    import tomllib

    bases: list[Path] = []
    py = tool_python()
    if py is not None:
        bases.append(py.parent.parent / "Lib" / "site-packages" / "finesub_cli"
                     / "_vendor" / "src" / "finesub_bootstrap")
    bases.append(_tool_dir() / "finesub" / "Lib" / "site-packages" / "finesub_cli"
                 / "_vendor" / "src" / "finesub_bootstrap")
    for base in bases:
        for name in ("pylock.win-py312.cn.toml", "pylock.win-py312.toml"):
            p = base / name
            if not p.is_file():
                continue
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 解析不了就换下一份
                continue
            total = 0
            for pkg in data.get("packages") or []:
                sizes = [int(w.get("size") or 0) for w in (pkg.get("wheels") or [])]
                size = max(sizes) if sizes else 0
                if size == 0 and str(pkg.get("name")) == "torch":
                    size = _TORCH_BYTES
                total += size
            if total > 0:
                # 下载进缓存 + 解包进 staging，落盘约是 wheel 的 2.4 倍
                return int(total * 2.4)
    return 6 * 2**30


class _RuntimeProgress:
    """引擎装运行环境时的进度（后台线程，只读磁盘、不碰子进程）

    引擎把下载交给 uv，uv 的输出接在管道里**没有进度条**，界面上原本只有一行
    「正在准备引擎运行环境（首次要下载 2~3 GB）」，几分钟不动 —— 用户以为卡死，
    反复关软件重开（2026-11-05 用户反馈：得让用户看得见初始化下载进度）。

    这里按磁盘增长估：`<home>/cache/uv`（下下来的 wheel）+ `<home>/runtime/python.staging`
    （正在解包的环境）。每 3 秒算一次百分比，每 12 秒报一句「已下 X GB / 约 Y GB」。
    """

    def __init__(self, params: dict, report):
        home = (str(params.get("finesub_home") or "").strip()
                or str(datadir.default_home()))
        self.home = Path(home)
        self.report = report
        self.total = max(1, _runtime_download_total())
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.interval = 3.0       # 多久看一眼磁盘
        self.message_every = 12.0  # 多久往日志里写一句「已下 X GB」

    def start(self) -> None:
        try:
            self.thread = threading.Thread(target=self._loop, daemon=True,
                                           name="engine-progress")
            self.thread.start()
        except Exception:  # noqa: BLE001 - 没有进度条也不能耽误安装
            self.thread = None

    def stop(self) -> None:
        self.stop_event.set()

    def _size(self) -> int:
        return (_dir_size(self.home / "cache" / "uv")
                + _dir_size(self.home / "runtime" / "python.staging"))

    def _loop(self) -> None:
        started = time.monotonic()
        last = self._size()
        last_msg = 0.0
        warned = False
        while not self.stop_event.wait(self.interval):
            got = self._size()
            pct = 2 + int(min(96, 94.0 * got / self.total))
            self.report("engine", pct, "")
            now = time.monotonic()
            if now - last_msg < self.message_every:
                continue
            speed = (got - last) / max(0.001, now - last_msg)
            last, last_msg = got, now
            text = (f"下载/安装运行环境：{got / 2**30:.1f} GB / 约 {self.total / 2**30:.1f} GB"
                    f"（{speed / 2**20:.1f} MB/s）")
            self.report("engine", pct, text)
            # ★ 用户要求：没梯子或者节点太慢时直接提示，别让人干等
            if not warned and got < 2 * 2**20 and now - started > 45:
                warned = True
                self.report("engine", pct,
                            "还没下到任何数据：如果没开代理，或者代理节点太慢，"
                            "请更换网络代理和节点后再试")


def _provision_runtime(params: dict, job: Job, report, finesub: str) -> bool:
    """装运行环境（带进度）；真正的流程在 _provision_runtime_impl 里"""
    progress = _RuntimeProgress(params, report)
    progress.start()
    try:
        return _provision_runtime_impl(params, job, report, finesub)
    finally:
        progress.stop()


def _provision_runtime_impl(params: dict, job: Job, report, finesub: str) -> bool:
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
    # ★ 先过微软运行库这一关（2026-10-23 客户报障）：
    #   torch 的 c10.dll 要从系统里拿 MSVCP140 / VCRUNTIME140，机器上那份太旧或没有，
    #   最后验证 `import torch` 时必然 WinError 1114。引擎一失败就把 staging 删掉，
    #   于是每次重试都要**把整套依赖重装一遍**（torch 的 wheel 有 uv 缓存，但
    #   重建 venv + 解包落盘也要好几分钟）—— 客户点了十几次，一次也没走完。
    #   这一关只读几个文件版本，几毫秒；不过关就当场装官方的（会弹一次 UAC）。
    import vcruntime
    if not vcruntime.ensure(params, report):
        report("engine", None,
               "⚠ 运行环境先不装了：这台机器缺微软 VC++ 运行库，torch 装上也加载不了。"
               "按上面的提示处理好之后，点「重新试一次」。")
        return False

    # 装之前先看一眼缓存：反复失败的机器上它会一直涨（见 prune_engine_cache）。
    # 放在这里而不是失败之后，是因为"上一轮"的垃圾最占地方，先清掉再下。
    prune_engine_cache(params, report)

    report("engine", None, "正在准备引擎运行环境（首次要下载 2~3 GB，请保持网络畅通、别关软件）…")
    env = _setup_env(params)
    # 说清楚引擎用的是哪个解释器：客户机器上有 32 位 Python 时，日志里必须看得出来
    # 我们绕开了它（否则再出问题又是一份「看不出用了哪个 Python」的诊断报告）。
    try:
        plan = engine_python_plan(params)
        if plan.get("python"):
            report("engine", None, f"引擎将使用 64 位 Python：{plan['python']}")
        for bad in plan.get("blocked") or []:
            report("engine", None,
                   f"已跳过系统里的 32 位 Python：{bad}"
                   "（引擎依赖只有 64 位版本，用它会装不上）")
    except Exception:  # noqa: BLE001 - 只是提示，出问题不影响安装
        pass

    # ★ 2026-11-05：先把引擎的必装资源（ffmpeg / tokcount / git）放到磁盘上。
    #   `finesub setup` 里只有 ffmpeg 是硬依赖，而它是整个流程里**唯一**还要问
    #   api.github.com 要 sha256、并且只能走单个镜像前缀下载的东西 —— 没代理的新用户
    #   就是卡在这一步。随包自带同一构建的 ffmpeg 后，这一步连网络都不碰。
    try:
        import runtime_pin as _pin
        ready = _pin.prefetch(params, report)
        if not ready.get("ffmpeg"):
            report("engine", None, "（随包 ffmpeg 没用上，改由引擎自己下载）")
    except Exception as e:  # noqa: BLE001 - 预置是加分项，不能拦住安装
        report("engine", None, f"（随包资源预置跳过：{e}）")

    tail: list[str] = []
    try:
        probe = env_probe(finesub)
        if probe["python"]:
            # 2026-11-05：客户报告里查不出用的是哪个解释器，只能让他再点一次。
            report("engine", None,
                   f"引擎解释器: {probe['python']}（{probe['version'] or '?'}）")
            if probe["import_ok"] is False:
                report("engine", None, f"（导入 pydantic_core 失败：{probe['error']}）")
        code = _stream(job, [finesub, "setup"], env, report, tail=tail)
        # ★ 环境坏了（导入阶段 DLL 加载失败）和「下载不动」是两回事：换下载源、
        #   等一小时都不可能好。直接重建工具环境（2026-11-05 客户报障：两天 67 次）。
        if code != 0 and env_broken(tail):
            report("engine", None,
                   "引擎的 Python 环境在导入组件时崩了（DLL 加载失败）——这不是网络问题，"
                   "自动重建一次环境…")
            if repair_env(params, job, report):
                env = _setup_env(params)
                try:
                    import runtime_pin as _pin2
                    _pin2.prefetch(params, report)
                except Exception:  # noqa: BLE001
                    pass
                tail.clear()
                code = _stream(job, [finesub, "setup"], env, report, tail=tail)
        # ★ 失败后换一条路再试一次（2026-11-05）：最常见的原因是**选中的镜像
        #   已经不回字节了**，而 mirrors.pick 的结果会缓存 6 小时。重探一遍镜像、
        #   或把钉死的 ffmpeg 回退成引擎默认路径，往往这一把就过了。
        #   明显的非网络失败（VC++ / c10.dll / 环境坏了）不重试，重试也只会再耗几分钟。
        retries = 0
        while code != 0 and retries < 2 and not env_broken(tail) and not any(
                ("1114" in s) or ("c10.dll" in s) for s in tail)                 and not bad_system_python(tail):
            action = _retry_route(params, report)
            if not action:
                break
            retries += 1
            report("engine", None, f"运行环境没装好，{action}后重试一次…")
            env = _setup_env(params)
            try:
                import runtime_pin as _pin
                _pin.prefetch(params, report)
            except Exception:  # noqa: BLE001
                pass
            tail.clear()
            code = _stream(job, [finesub, "setup"], env, report, tail=tail)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        report("engine", None, f"（运行环境预置跳过：{e}）")
        return False
    if code == 0:
        _set_setup_reason("")
        report("engine", None, "✅ 引擎运行环境已就绪")
        return True
    # 这次失败留下的轮子也一并收拾（超阈值才动手，见 prune_engine_cache）
    prune_engine_cache(params, report)
    # 退出码后面什么都不说，用户只能看到一行「没准备好」。引擎的失败原因都印在
    # 输出的最后几行里，认得出就翻译成人话（见 _stream 的 tail）。
    if env_broken(tail):
        report("engine", None,
               "⚠ 运行环境在这台机器上加载不了：引擎的 Python 组件导入就崩（DLL 加载失败）。"
               "这不是下载慢，重试和等待都不会好。最可能是安全软件拦了 Python 的 DLL —— "
               "把程序目录、%LOCALAPPDATA%\\uv 和引擎数据目录加入白名单"
               "（或临时退出杀软）后，再点「重新试一次」。")
        _set_setup_reason("引擎的 Python 组件导入就崩（DLL 加载失败），最可能是安全软件拦了 Python 的 DLL。")
        return False
    if any(("1114" in s) or ("c10.dll" in s) for s in tail):
        report("engine", None,
               "⚠ 运行环境卡在加载 torch（WinError 1114 / c10.dll）：这是微软 VC++ 运行库的问题，"
               "不是网络也不是显卡。程序已经尝试自动修；如果还不行，"
               "重启电脑后再点「重新试一次」，或手动装一次 https://aka.ms/vc14/vc_redist.x64.exe。")
        _set_setup_reason("卡在加载 torch（WinError 1114 / c10.dll）——这是微软 VC++ 运行库的问题。")
        return False
    if bad_system_python(tail):
        report("engine", None,
               "⚠ 引擎想复用你系统里的 Python 3.12，但它是 **32 位** 的 —— 依赖组件"
               "（onnxruntime）只有 64 位版本，装上去也加载不了。已经改成用引擎自带的 "
               "64 位 Python，再点一次「重新试一次」就会重装；如果反复出现，请把"
               "「Python312-32」卸载掉（或从 PATH 里移除）。")
        _set_setup_reason("系统里的 Python 3.12 是 32 位版本，引擎依赖只有 64 位；"
                          "已改用引擎自带的 64 位 Python，请再点一次「重新试一次」。")
        return False
    if network_broken(tail):
        report("engine", None,
               "⚠ 下载没走完：网络连不上，或者代理节点太慢。请更换网络代理和节点后"
               "再点「重新试一次」；没有代理的机器建议先开代理（首次要下 2~3 GB）。")
        _set_setup_reason("网络连不上或代理节点太慢：请更换网络代理和节点后重试。")
        return False
    report("engine", None,
           f"⚠ 运行环境这次没准备好（退出码 {code}）——第一次翻译时会自动再试一次。"
           f"如果一直失败，请把诊断报告发给我们。")
    return False


def _retry_route(params: dict, report) -> str:
    """运行环境失败后换一条路走。返回一句人话；空串 = 没有可换的路。

    两种可换的路（2026-11-05 客户集中报障后加的）：
      1. 钉死的 ffmpeg 下不动 → 还原成引擎原来的清单（下次预置会按新布局重新放文件）；
      2. 选中的 GitHub 加速镜像已经死了 → 强制重探一遍，换一个还活着的。
    `mirrors.pick` 的结果会缓存 6 小时，不强制重探的话第二次跑还是同一个死镜像。
    """
    try:
        import runtime_pin as _pin
        if not _pin.ffmpeg_ready(params) and _pin.restore():
            return "把 ffmpeg 改回引擎默认下载路径"
    except Exception:  # noqa: BLE001 - 换路失败就按没得换处理
        pass
    try:
        old = mirrors.pick(params)
        new = mirrors.pick(params, force=True)
        if new != old:
            return f"换下载镜像（{new or '官方源'}）"
    except Exception:  # noqa: BLE001
        pass
    return ""
