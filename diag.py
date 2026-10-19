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
    return "(没有 NVIDIA 显卡或驱动，引擎会走 CPU——会明显慢)"


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
            state = (f"已下 {done / 1048576:.0f} MB"
                     + (f"，下到一半 {psize / 1048576:.0f} MB" if part else "，完整"))
            lines.append(f"    {repo.name}: {state}")
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
    base = Path(sys.executable).parent if tools.is_packaged() else Path(__file__).parent
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


def environment_info(deep: bool = False) -> str:
    """环境快照（key 只报有无，不报内容）

    deep=True 时多跑几项"会动磁盘"的检查（目前是引擎数据目录能不能建目录链接），
    只在导出诊断报告时用——开机就探一遍会白白在人家各个盘上建删目录。
    """
    import tools
    # ⚠️ 用 tools.is_packaged()：Nuitka 不设 sys.frozen，直接读它的话，
    #    用户诊断报告里会写着「打包运行: False」，把人往「这不是正式版？」上带。
    frozen = tools.is_packaged()
    base = Path(sys.executable).parent if frozen else Path(__file__).parent
    cfg_now: dict | None = None          # 后面的段落（数据目录 / 外网依赖 / 模型路由）都要用
    lines = [
        "===== 环境信息 =====",
        f"时间: {_now()}",
        f"应用: 星译（StarryVT）",
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
        lines.append("----- 引擎模型缓存 -----")
        lines += _hf_cache_report()
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读模型缓存失败: {e})")
    try:
        lines.append("----- 下载路由 -----")
        lines += _routes_report()
    except Exception as e:  # noqa: BLE001
        lines.append(f"(读下载路由失败: {e})")
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
        lines += [
            "----- 配置 -----",
            f"finesub_exe: {c.get('finesub_exe') or '(默认 PATH)'}",
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
        st = engine.status()
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
            lines += datadir.describe()
        else:
            lines.append("(各目录可用性：导出诊断报告时检测)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"(检测引擎数据目录失败: {e})")
    try:
        # 外网依赖：哪几样只能从 GitHub 拿、现在走什么路子（无代理用户的排障关键）
        import mirrors
        import wheels
        lines.append("----- 外网依赖（GitHub）-----")
        lines += mirrors.describe(cfg_now or {})
        lines += wheels.describe()
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
        exe = engine.find_finesub("")
        if not exe:
            return ""
        r = subprocess.run([exe, "doctor"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           stdin=subprocess.DEVNULL,
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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write(f"===== 会话开始 v{app_version} =====", "boot")
    for line in environment_info().splitlines():
        write(line, "boot")
