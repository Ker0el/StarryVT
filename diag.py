#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断日志：运行日志 / 异常堆栈 / 环境信息 统一写到 %APPDATA%\\StarryVT\\logs\\

- 运行日志：logs\\app-YYYYMMDD.log（按天一个文件）
- 崩溃日志：logs\\crash-时间戳.log（含完整堆栈 + 环境信息，方便排查）
- 诊断报告：一键打包（环境信息 + 全部日志，**不含任何 key**）便于发给开发者
"""
from __future__ import annotations

import datetime
import os
import platform
import subprocess
import sys
import threading
import traceback
import zipfile
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT"
LOG_DIR = APP_DIR / "logs"

#: 应用版本（start_session 里由 app.py 传进来；诊断报告的第一屏要能看到）
_APP_VERSION = ""


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------- 硬件 / 磁盘（判"是慢还是卡"必须有这些）----------------

def _cpu_name() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor() or "(读不到)"


def _memory() -> str:
    """物理内存总量 / 可用量（GB）。ctypes 直接问系统，不依赖任何第三方包。"""
    if os.name != "nt":
        return "(非 Windows)"
    try:
        import ctypes

        class _MEMSTATUS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return "(读不到)"
        return (f"共 {st.ullTotalPhys / 1024 ** 3:.1f} GB，"
                f"可用 {st.ullAvailPhys / 1024 ** 3:.1f} GB")
    except Exception as e:  # noqa: BLE001
        return f"(读不到: {e})"


def _gpu() -> str:
    """显卡。**「卡在语音识别」十有八九要看这一行**：

    引擎默认按机器有没有 CUDA 设备决定跑 GPU 还是 CPU，而 CPU 转写要慢一两个数量级。
    nvidia-smi 是唯一稳的取法（不装 CUDA 就没有，那也是答案本身）。

    ★ 但 nvidia-smi 只说「有没有 NVIDIA」，说不出「装的是什么卡」
      （2026-10-26 客户报障：AMD 机器上报告只有一句「没有 NVIDIA 显卡或驱动」，
      客户以为显卡没被识别）。没有 NVIDIA 时补一行注册表里的真实型号，
      并明确写清「引擎只支持 NVIDIA CUDA，会用 CPU」。
    """
    import shutil
    import subprocess
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        r = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=0x08000000)          # CREATE_NO_WINDOW
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return "；".join(line.strip() for line in out.splitlines() if line.strip())
    except Exception:
        pass
    try:
        import tools
        adapters = tools.display_adapters()
    except Exception:  # noqa: BLE001
        adapters = []
    if adapters:
        names = "、".join(adapters)
        if any("nvidia" in a.lower() for a in adapters):
            return f"{names}（NVIDIA 驱动 / nvidia-smi 不可用，引擎可能回落到 CPU）"
        return f"{names}（引擎只支持 NVIDIA CUDA，会用 CPU——会明显慢，不是故障）"
    return "(没有 NVIDIA 显卡或驱动，引擎会走 CPU——会明显慢)"


def _repo_model_id(repo_dir: str) -> str:
    """`models--org--name` → 清单里的 model_id（认不出就空串，报告里只少一句注解）"""
    try:
        import modelfetch
        for mid, spec in (modelfetch._models() or {}).items():
            repo = (spec or {}).get("repo")
            if repo and "models--" + repo.replace("/", "--") == repo_dir:
                return mid
    except Exception:  # noqa: BLE001 - 报告不能因为注解而失败
        pass
    return ""


def _model_ready(root: Path, model_id: str) -> bool:
    """这个模型在这个引擎目录里，引擎到底加载得了吗（stat 判据，不联网）"""
    try:
        import modelfetch
        return modelfetch.ready({"finesub_home": str(root)}, model_id)
    except Exception:  # noqa: BLE001
        return True                      # 判不了就别吓人


def _hf_cache_report() -> list[str]:
    """引擎已下载的模型缓存：目录名 / 大小 / 有没有下到一半的 .incomplete

    2026-10-08 客户的「卡在 [2/4] 语音识别 不动」就是卡在这儿——引擎第一次要下
    whisper 权重（1.62GB），而这一步**全程没有任何输出**。这份清单是当时唯一能
    指出"它在下什么"的东西，报告里必须带上。
    """
    import datadir
    roots: list[Path] = []
    for name in ("finesub_home",):
        try:
            import settings as cfg
            v = (cfg.load().get(name) or "").strip()
            if v:
                roots.append(Path(v))
        except Exception:
            pass
    roots.append(datadir.default_home())
    seen: set[str] = set()
    lines: list[str] = []
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        hub = root / "models" / "huggingface" / "hub"
        if not hub.is_dir():
            lines.append(f"{root}: (没有 models\\huggingface\\hub)")
            continue
        repos = sorted(p for p in hub.glob("models--*") if p.is_dir())
        if not repos:
            lines.append(f"{root}: 没有已下载的模型")
            continue
        lines.append(str(root))
        for repo in repos:
            blobs = list((repo / "blobs").glob("*")) if (repo / "blobs").is_dir() else []
            done = sum(b.stat().st_size for b in blobs if b.is_file()
                       and not b.name.endswith(".incomplete"))
            part = [b for b in blobs if b.name.endswith(".incomplete")]
            psize = sum(b.stat().st_size for b in part)
            if not done and not part:
                # 0 字节 ≠ 完整：这是只建了个目录、一个文件都没下到（旧版预取中断，
                # 或者引擎自己建的壳）。以前这里写「完整」，报告看着像没事，
                # 实际开跑就卡在 `Cannot load the vocabulary`。
                state = "空目录（一个文件都没下到）"
            else:
                state = (f"已下 {done / 1048576:.0f} MB"
                         + (f"，下到一半 {psize / 1048576:.0f} MB" if part else "，完整"))
                # ★ 2026-11-07：「完整」只能说明**字节够了**，不代表引擎加载得了。
                #   诊断报告-20261107-143642 就是被这一行骗的：这里写着「1547 MB，完整」，
                #   而引擎每次都在语音识别那步报 LocalEntryNotFoundError。两个仓库
                #   （识别 / 校验）按引擎自己的判据再 stat 一遍，对不上就写明白。
                if not part:
                    mid = _repo_model_id(repo.name)
                    if mid and not _model_ready(root, mid):
                        state += "，但加载不了（快照/小文件对不上，引擎会当成没下过）"
            lines.append(f"    {repo.name}: {state}")
    # 引擎到底去哪个目录找模型（`HF_HOME`）。2026-11-07 那次报障全靠猜：界面这份齐了，
    # 引擎那份空的，两边目录分叉。以后报告里直接写出来。
    try:
        import modelfetch
        pinned = modelfetch.hf_env({"finesub_home": str(roots[0]) if roots else ""})
        lines.append("引擎 HF_HOME: " + (
            pinned["HF_HOME"] if pinned
            else "(未钉 —— 我们这份没下齐，引擎按它自己的规则找)"))
    except Exception:  # noqa: BLE001 - 报告不能因为这一行而失败
        pass
    return lines or ["(无)"]


def _disk_report() -> list[str]:
    import shutil
    out: list[str] = []
    seen: set[str] = set()
    cands: list[str] = []
    try:
        import datadir
        import settings as cfg
        v = (cfg.load().get("finesub_home") or "").strip()
        if v:
            cands.append(v)
        cands.append(str(datadir.default_home()))
    except Exception:
        pass
    import tools
    base = tools.exec_path().parent if tools.is_packaged() else Path(__file__).parent
    cands.append(str(base))
    for c in cands:
        anchor = Path(c).anchor or c
        if anchor.lower() in seen:
            continue
        seen.add(anchor.lower())
        try:
            u = shutil.disk_usage(anchor)
            out.append(f"{anchor} 剩余 {u.free / 1024 ** 3:.1f} GB / 共 {u.total / 1024 ** 3:.1f} GB")
        except Exception as e:  # noqa: BLE001
            out.append(f"{anchor} 读不到: {e}")
    return out


def session_log() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"app-{datetime.date.today():%Y%m%d}.log"


def write(line: str, tag: str = "app"):
    """写一行运行日志（失败也不影响主流程）"""
    try:
        with open(session_log(), "a", encoding="utf-8") as fh:
            fh.write(f"{_now()} [{tag}] {line}\n")
    except Exception:
        pass


def _routes_report() -> list[str]:
    """引擎的下载路由状态（`%LOCALAPPDATA%\\FineSub\\download-routes.json`）

    它记着「这台机器有没有放弃某个镜像」：`failures.huggingface` 累计到 3 次就
    `is_degraded` → 之后**一律走官方源**，而国内直连 huggingface.co 是死的。
    2026-10-08 客户「卡在取 whisper 权重」时，这是唯一能判断镜像有没有被拉黑的东西
    —— 报告里没有它，就只能靠猜。文件很小，直接整份带上。

    ⚠️ 它固定在 `%LOCALAPPDATA%\\FineSub`，**不跟 FINESUB_HOME 走**
    （见 `finesub_bootstrap/paths.py: default_data_root()`）。
    """
    import json
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    p = Path(local) / "FineSub" / "download-routes.json"
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{p}: (还没有这个文件)"]
    except Exception as e:  # noqa: BLE001
        return [f"{p}: 读不了（{type(e).__name__}: {e}）"]
    return [f"{p}:", "    " + json.dumps(body, ensure_ascii=False)]


def _version_of(path: Path) -> str:
    """文件版本号（读不到返回 ""）—— 判 DLL 新旧用，不需要管理员权限"""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version", use_last_error=True)
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return ""
        # VS_FIXEDFILEINFO：dwFileVersionMS/LS（两个 DWORD，各拆高低 16 位）
        fixed = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return ""


def _engine_env_report() -> list[str]:
    """引擎的 Python 环境：用的是哪个解释器、编译扩展能不能导入

    ★ 2026-11-05 客户报告：两天 67 次「运行环境没准备好」，每次都死在
      `ImportError: DLL load failed while importing _pydantic_core: 找不到指定的程序`
      （WinError 127 = 依赖 DLL 里没有它要的导出，不是下载问题），可报告里
      **看不出用的是哪个 Python**，只能请客户再点一次。这里补上，下次一锤定音。
    """
    out = ["----- 引擎 Python 环境 -----"]
    try:
        import settings as cfg2
        import datadir
        home = (str((cfg2.load() or {}).get("finesub_home") or "").strip()
                or str(datadir.default_home()))
        out.append(f"数据目录: {home}" + ("" if home.isascii() else "  ⚠ 含非英文字符"))
        try:
            import engine as _e
            out.append(f"托管 Python 目录: {_e._uv_python_dir({'finesub_home': home})}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        out.append(f"(读引擎数据目录失败: {e})")
    try:
        import engine
    except Exception as e:  # noqa: BLE001
        return out + [f"(导入 engine 失败: {e})"]
    try:
        py = engine.tool_python()
    except Exception as e:  # noqa: BLE001
        return out + [f"(读工具环境失败: {e})"]
    if not py:
        return out + ["工具环境: (没找到 finesub 的工具环境)"]
    root = py.parent.parent
    out.append(f"工具环境: {root}")
    out.append(f"解释器: {py}")
    # pyvenv.cfg 的 home = 解释器实际来自哪个目录（uv 托管 / 系统 Python）
    try:
        cfg = (root / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
        for line in cfg.splitlines():
            if line.strip().lower().startswith("home"):
                out.append("pyvenv.cfg → " + line.strip())
                break
    except OSError:
        pass
    # 引擎建运行环境时会用哪个解释器、哪些路径被挡掉了
    # （2026-11-05 客户机器上是个 32 位 Python 3.12.0a2，引擎复用它 → onnxruntime
    #   没有 32 位轮子 → 运行环境永远装不完，报告里必须能一眼看出来）
    try:
        cfg_mod = locals().get("cfg2")
        home = (str((cfg_mod.load() or {}).get("finesub_home") or "").strip()
                if cfg_mod is not None else "")
        plan = engine.engine_python_plan({"finesub_home": home})
        out.append("引擎建运行环境将用: "
                   + (plan.get("python") or "(由引擎自己下载 64 位 Python 3.12)"))
        for bad in plan.get("blocked") or []:
            out.append(f"已挡掉的 Python 路径: {bad}")
    except Exception as e:  # noqa: BLE001 - 只是报告，不能把诊断搞崩
        out.append(f"(算引擎用哪个 Python 失败: {e})")
    probe = engine.env_probe()
    out.append(f"版本: {probe.get('version') or '(读不到)'}"
               f"  base_prefix: {probe.get('base_prefix') or '?'}")
    if probe.get("import_ok") is None:
        out.append(f"导入 pydantic_core: (探针没跑起来：{probe.get('error') or '未知'})")
    elif probe.get("import_ok"):
        out.append("导入 pydantic_core: 正常")
    else:
        out.append(f"导入 pydantic_core: 失败 — {probe.get('error') or ''}")
    # 编译扩展本身：文件名/大小/sha256，对着它才能判断是不是装坏了
    try:
        import hashlib
        for p in sorted((root / "Lib" / "site-packages" / "pydantic_core")
                        .glob("_pydantic_core*.pyd")):
            h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            out.append(f"pyd: {p.name}  {p.stat().st_size} B  sha256={h}…")
    except OSError:
        pass
    # 解释器目录里的 python3xx.dll / 运行库：127 就是它们的版本对不上
    base = Path(probe.get("base_prefix") or "")
    if base and base.is_dir():
        for name in ("python312.dll", "python311.dll", "python313.dll",
                     "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
            f = base / name
            if f.is_file():
                v = _version_of(f)
                out.append(f"{name}: {v or '(无版本信息)'}  {f}")
    # 运行环境的下载源：官方源传不动时会临时改到国内镜像（见 pypi_mirror）
    try:
        import pypi_mirror
        out.append(f"运行环境下载源: {pypi_mirror.describe()}")
    except Exception:  # noqa: BLE001 - 只是补充信息
        pass
    return out


def environment_info(deep: bool = False) -> str:
    """环境快照（key 只报有无，不报内容）

    deep=True 时多跑几项"会动磁盘"的检查（目前是引擎数据目录能不能建目录链接），
    只在导出诊断报告时用——开机就探一遍会白白在人家各个盘上建删目录。
    """
    import tools
    # ⚠️ 用 tools.is_packaged()：Nuitka 不设 sys.frozen，直接读它的话，
    #    用户诊断报告里会写着「打包运行: False」，把人往「这不是正式版？」上带。
    frozen = tools.is_packaged()
    base = tools.exec_path().parent if frozen else Path(__file__).parent
    cfg_now: dict | None = None          # 后面的段落（数据目录 / 外网依赖 / 模型路由）都要用
    lines = [
        "===== 环境信息 =====",
        f"时间: {_now()}",
        f"应用: 星译（StarryVT） v{_APP_VERSION or '(未记录)'}",
        f"系统: {platform.system()} {platform.release()} 版本 {platform.version()}",
        f"Python: {sys.version.split()[0]}（打包运行: {frozen}）",
        f"程序目录: {base}",
        f"配置目录: {APP_DIR}",
        "----- 硬件与磁盘 -----",
        f"CPU: {_cpu_name()}",
        f"内存: {_memory()}",
        f"显卡: {_gpu()}",
    ]
    # 磁盘和"引擎下过哪些模型"都要动磁盘、还要读设置，放这里单独兜一层
    try:
        lines += [f"磁盘: {line}" for line in _disk_report()]
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读磁盘失败: {e})")
    try:
        lines.append("----- 数据目录 -----")
        import paths
        info = paths.info(with_sizes=deep)
        lines.append(f"根目录: {info['root']}"
                     + ("（自定义）" if info["custom"] else "（系统默认）"))
        for name, gb in info["items_gb"].items():
            p = Path(info["root"]) / name
            if gb is None:
                if p.is_dir():
                    lines.append(f"  {name}: {p}")
            elif gb:
                lines.append(f"  {name}: {gb} GB")
        for name, p in info["legacy"].items():
            if Path(p).is_dir():
                lines.append(f"  ⚠ 旧位置还有 {name}: {p}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读数据目录失败: {e})")
    try:
        lines.append("----- 本地语音克隆 -----")
        import localclone
        _cl = localclone.status()
        lines.append(f"状态: {_cl['detail'].splitlines()[0]}")
        if _cl.get("missing"):
            lines.append(f"缺文件: {'、'.join(_cl['missing'])}")
        lines.append(f"位置: {_cl['dir']}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读本地语音克隆失败: {e})")
    try:
        lines.append("----- 引擎模型缓存 -----")
        lines += _hf_cache_report()
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读模型缓存失败: {e})")
    try:
        # 下载缓存单独报一行：安装失败重试会让它无限涨（2026-10-23 客户那台 40.4 GB，
        # 模型才 3.5 GB）。正常装好之后它和运行环境是硬链接，实际多占不到 1 GB。
        import engine as _eng
        import settings as cfg2
        _cb = _eng.engine_cache_bytes(cfg2.load())
        lines.append(f"引擎下载缓存: {_cb / 2**30:.1f} GB"
                     f"（超过 {_eng.CACHE_PRUNE_GB:.0f} GB 会自动清理）")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读下载缓存失败: {e})")
    try:
        lines.append("----- 下载路由 -----")
        lines += _routes_report()
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读下载路由失败: {e})")
    exe_hint = ""              # 下面两个 try 块都要用；读配置失败时也不能 NameError
    try:
        import settings as cfg
        c = cfg.load()
        keys = {k: ("已配置" if c.get(k) else "未配置")
                for k in ("mimo_key", "deepseek_key", "gemini_key")}
        # 输出目录要报「实际会写到哪」：客户报"改了输出目录还是不对"时，
        # 只看配置里空不空没有意义，得看真正生效的路径（见 pipeline.resolve_output_base）
        try:
            from pipeline import resolve_output_base
            out_cfg = (c.get("output_dir") or "").strip()
            # create=False：导出诊断报告不该顺手在用户视频旁边建目录
            out_real = str(resolve_output_base(c, create=False))
        except Exception as e:  # noqa: BLE001
            out_cfg, out_real = (c.get("output_dir") or "").strip(), f"(解析失败: {e})"
        # 「翻译引擎路径」填成目录时，Popen(目录) 只会报 WinError 5（2026-10-25 客户）。
        # 报告里直接点出来，省得每次都要从崩溃日志里猜。
        exe_hint = (c.get("finesub_exe") or "").strip()
        exe_shown = exe_hint or "(默认 PATH)"
        if exe_hint:
            try:
                if Path(exe_hint).is_dir():
                    exe_shown += "  ⚠ 这是个目录（应填 finesub.exe；留空 = 自动查找）"
            except OSError:
                pass
        lines += [
            "----- 配置 -----",
            f"finesub_exe: {exe_shown}",
            f"finesub_home: {c.get('finesub_home') or '(引擎默认)'}",
            f"代理: {c.get('proxy') or '(无)'}",
            f"HF 镜像: {c.get('hf_endpoint') or '(无)'}",
            f"音色/源语言: {c.get('voice')} / {c.get('source_lang')}",
            f"原声音量: {c.get('orig_vol')}",
            # 这两个增强要连 Google（上传画面 / 联网搜索）：没代理的机器开了就是白等。
            # 2026-10-08 排查客户「纠错翻译阶段一路 Gemini upload failed」时，
            # 报告里看不到开关状态，只能从引擎日志反推 —— 补上。
            f"可选增强: 视频语境纠错={'开' if c.get('video_context') else '关'}"
            f" / 联网查证={'开' if c.get('web_search') else '关'}"
            f" / DeepSeek联网查证={'开' if c.get('deepseek_web_search') else '关'}",
            f"字幕检查: {'开' if c.get('subtitle_review') else '关'}",
            # ⚠️ 这行只报「用户填的那一项」。留空 = 走代码里的顺序（自建源优先、GitHub 兜底），
            #    所以别写成"更新地址: (默认)"让人以为默认就是 GitHub —— 实际试了哪几个源，
            #    看下面「检查更新：…（源 X，共试了 N 个）」那几行。
            f"更新地址: {c.get('update_url') or '(留空 → 自建源优先，GitHub 兜底)'}",
            # 输出位置现在跟输入走（本地视频旁边 / 链接走软件目录），所以输入也要一起报
            f"本次输入: {c.get('source') or '(空)'}",
            f"输出目录: {out_cfg or '(留空=跟随输入)'} → 实际写入 {out_real}",
            f"Key: MiMo={keys['mimo_key']} DeepSeek={keys['deepseek_key']} Gemini={keys['gemini_key']}",
        ]
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读取配置失败: {e})")
    try:
        import engine
        # 带设置里的提示查：报「实际会用的那个引擎」，不再是 PATH 上的另一个
        st = engine.status(exe_hint)
        lines += [
            "----- 翻译引擎 -----",
            f"finesub: {st.get('engine_path') or '(未找到)'}",
            f"uv: {st.get('uv_path') or '(未找到)'}",
        ]
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测引擎失败: {e})")
    try:
        # 引擎数据目录：排查 os error 448（不受信任的装入点）的关键信息
        import datadir
        cfg_now = None
        try:
            import settings as cfg2
            cfg_now = cfg2.load()
        except Exception:  # noqa: BLE001
            pass
        used = ((cfg_now or {}).get("finesub_home") or "").strip() or str(datadir.default_home())
        lines.append("----- 引擎数据目录 -----")
        lines.append(f"当前使用: {used}")
        bad = datadir.bad_homes()
        if bad:
            lines.append("建过链接失败（已排除）: " + "；".join(bad))
        if deep:
            lines += datadir.describe(used)
        else:
            lines.append("(各目录可用性：导出诊断报告时检测)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测引擎数据目录失败: {e})")
    try:
        # 引擎工具环境用的哪个 Python、能不能导入编译扩展（2026-11-05 客户报障后加的）
        lines += _engine_env_report()
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测引擎 Python 环境失败: {e})")
    try:
        # 外网依赖：哪几样只能从 GitHub 拿、现在走什么路子（无代理用户的排障关键）
        import mirrors
        import wheels
        lines.append("----- 外网依赖（GitHub）-----")
        lines += mirrors.describe(cfg_now or {})
        lines += wheels.describe()
        # 引擎运行环境的三条路线（2026-11-05 新用户集中报「运行环境还没准备好」）：
        # ffmpeg 是 setup 里唯一的硬依赖，也是唯一要问 api.github.com 的东西。
        try:
            import runtime_pin
            st = runtime_pin.status()
            home = str((cfg_now or {}).get("finesub_home") or "").strip()
            if not home:
                import datadir
                home = str(datadir.default_home())
            ready = runtime_pin.ffmpeg_ready({"finesub_home": home})
            if ready:
                lines.append("ffmpeg 下载路线: 已就绪（随包预置／已下载，setup 不需要再下）")
            elif not st["files"]:
                lines.append("ffmpeg 下载路线: 找不到引擎清单（引擎还没装好）")
            elif any(f.get("pinned") for f in st["files"]):
                lines.append("ffmpeg 下载路线: 已钉死到日期构建（不问 GitHub API，只下载文件）")
            else:
                lines.append("ffmpeg 下载路线: 未钉死 —— 会请求 GitHub API（限流时会装不上）")
            lines.append("引擎 ffmpeg 已就绪: " +
                         ("是" if ready else "否（setup 时仍需下载 194MB）"))
        except Exception as e:  # noqa: BLE001
            lines.append(f"(检测 ffmpeg 路线失败: {e})")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测外网依赖失败: {e})")
    try:
        # 翻译模型路由：引擎自带的默认路由只有 Gemini（国内直连不可用），实际走哪家
        # 由我们生成的路由文件决定。客户报"翻译不出来"时先看这一段。见 llmroute.py
        import llmroute
        lines += llmroute.describe((cfg_now or {}))
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测翻译模型路由失败: {e})")
    try:
        # 应用内更新记录：客户说"更新完没打开 / 版本没变"时，这条时间线是唯一证据
        ulog = LOG_DIR / "update.log"
        lines.append("----- 应用内更新记录 -----")
        if ulog.is_file():
            tail = ulog.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            lines += tail[-8:] or ["(空)"]
        else:
            lines.append("(没有 logs\\update.log：本机没跑过应用内更新，多半是手动装的安装包)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读取更新记录失败: {e})")
    try:
        # 随包组件：排查"找不到文件"类问题的关键信息（缺哪个、去哪找过）
        import tools
        ck = tools.self_check()
        lines.append("----- 随包组件 -----")
        for name, p in ck["found"].items():
            lines.append(f"{name}: {p or '❌ 未找到'}")
        lines.append(f"查找目录: {' | '.join(ck['dirs'])}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测随包组件失败: {e})")
    try:
        # 微软运行库：torch 的 c10.dll 运行时要从系统里拿这几支，缺/旧就会
        # 报 WinError 1114「DLL 初始化例程失败」（看着像 torch 坏了）。
        # 2026-10-23 客户那台机器就是栽在这儿，报告里必须能一眼看到版本。
        import vcruntime
        lines.append("----- 微软运行库（torch 依赖）-----")
        lines.append(vcruntime.summary())
        lines.append(f"判定: {vcruntime.problem() or '正常'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测运行库失败: {e})")
    return "\n".join(lines) + "\n"


def write_exception(exc: BaseException, context: str = "", detail: str = "") -> Path | None:
    """把异常（含完整堆栈）写成独立崩溃日志，返回文件路径

    detail 是调用方补的现场信息（子进程退出码、最后若干行输出…）。**这个参数很关键**：
    引擎有很多失败是"子进程没吐任何 Python 堆栈就死了"，光有异常类型等于没线索——
    2026-10-08 客户那次 `[2/4] 语音识别` 卡死就是这么丢的。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        p = LOG_DIR / f"crash-{_stamp()}.log"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(environment_info())
            fh.write(f"\n===== 异常 =====\n上下文: {context}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
            if detail:
                fh.write(f"\n===== 现场 =====\n{detail}\n")
        write(f"❌ [{context}] {type(exc).__name__}: {exc} → 详见 {p.name}", "error")
        return p
    except Exception:
        return None


def engine_log_dirs() -> list[Path]:
    """引擎自己的运行日志目录（可能有多个候选）

    ⚠️ **必须收进诊断报告**：引擎每次运行都会写一份**逐阶段 verbose 日志**到这个目录
    （`run-<时间戳>-<名字>.log`，行缓冲，进程被杀也留得下）。它记着阶段耗时、ASR 每段
    进度、模型取用、资源占用——我们光是靠 stdout 抓到的那些行根本不够用。
    路径规则见引擎的 `finesub/paths.py:resolve_logs_dir`：**用户数据固定放
    %LOCALAPPDATA%\\FineSub\\user-data**，跟 FINESUB_HOME 改到哪个盘无关。
    """
    cands: list[Path] = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "FineSub" / "user-data" / "logs")
    try:                                    # 万一是别的布局，把 FINESUB_HOME 下也扫一遍
        import settings as cfg
        home = (cfg.load().get("finesub_home") or "").strip()
        if home:
            cands.append(Path(home) / "user-data" / "logs")
    except Exception:
        pass
    out, seen = [], set()
    for d in cands:
        if d.is_dir() and str(d).lower() not in seen:
            seen.add(str(d).lower())
            out.append(d)
    return out


def engine_run_logs(limit: int = 8) -> list[Path]:
    """引擎最近几次运行的日志（按修改时间倒序）"""
    found: list[Path] = []
    for d in engine_log_dirs():
        try:
            found += [p for p in d.glob("run-*.log") if p.is_file()]
        except OSError:
            pass
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def engine_doctor() -> str:
    """`finesub doctor` 的输出（引擎自报的路径与状态）

    它把数据目录、运行时、模型、路由的**真实**位置一次打全——客户报"装不上/找不到"
    时，这一份比我们猜十次都准。拿不到就返回空串。"""
    try:
        import engine
        import job as jobmod
        # ★ 必须带上和真正跑引擎时同一套环境（2026-10-25 客户报障）：
        #   不传 FINESUB_HOME 时引擎按自己的默认 home 找 runtime / ffmpeg，
        #   数据目录已经搬到 D:\... 的机器会误报「runtime missing」「ffmpeg missing」，
        #   把排查引到错误的方向。
        try:
            import settings as cfg
            c = cfg.load() or {}
        except Exception:  # noqa: BLE001 - 读不到配置也要能出报告
            c = {}
        exe = engine.find_finesub(str(c.get("finesub_exe") or ""))
        if not exe:
            return ""
        env = dict(os.environ)
        home = str(c.get("finesub_home") or "").strip()
        if home:
            env["FINESUB_HOME"] = home
        env["FINESUB_HF_ENDPOINT"] = c.get("hf_endpoint") or "https://hf-mirror.com"
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run([exe, "doctor"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           stdin=subprocess.DEVNULL, env=env,
                           creationflags=getattr(jobmod, "CREATE_NO_WINDOW", 0))
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception:
        return ""


def export_report() -> Path:
    """打包诊断报告 zip（环境信息 + 我们的全部日志 + 引擎的运行日志，不含 key）"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = APP_DIR / f"诊断报告-{_stamp()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("环境信息.txt", environment_info(deep=True))
        for f in sorted(LOG_DIR.glob("*.log")):
            try:
                z.write(f, f"logs/{f.name}")
            except Exception:
                pass

        # 引擎自己的 verbose 运行日志：没有它，"卡在语音识别"这类问题只能靠猜。
        # 单独放 engine-logs/ 目录，免得跟我们的 app-*.log 混在一起。
        for f in engine_run_logs():
            try:
                z.write(f, f"engine-logs/{f.name}")
            except Exception:
                pass

        doc = engine_doctor()
        if doc:
            z.writestr("finesub-doctor.txt", doc + "\n")
    return out


def install_hooks():
    """把未捕获异常也写进崩溃日志（主线程 + 子线程）"""
    def _hook(exc_type, exc, tb):
        write_exception(exc, "未捕获异常（主线程）")
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = _hook

    def _thread_hook(args):
        if args.exc_value is not None:
            name = getattr(args.thread, "name", "?")
            write_exception(args.exc_value, f"未捕获异常（线程 {name}）")
    threading.excepthook = _thread_hook


def start_session(app_version: str = "0.1.0"):
    """启动时记一笔：环境快照 + 会话开始"""
    global _APP_VERSION
    # ★ 2026-11-11：「应用: 星译（StarryVT）」这一行没有版本号，客户发来诊断报告
    #   只能靠 traceback 的行号去源码里反推是哪一版（那次就是靠 app.py:894 认出的）。
    _APP_VERSION = str(app_version or "").strip()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write(f"===== 会话开始 v{app_version} =====", "boot")
    for line in environment_info().splitlines():
        write(line, "boot")
