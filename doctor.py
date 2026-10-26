#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境自检：把用户机器上容易出问题的地方一次查完，用大白话讲清楚

用户不该靠翻日志排错（报障时他们只会说"用不了"）。这里把每一项查成
✅ 正常 / ⚠ 有隐患 / ❌ 需要处理，并给出能点的按钮；
能自动修的（引擎数据目录 os error 448、缺组件的定位）当场修掉，用户无感。

deep=True 时额外做联网检查（翻译服务通不通），只在用户主动点「环境自检」时跑，
启动时的快检不带网络——不能让用户开着软件就转圈。
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OK, WARN, BAD = "ok", "warn", "bad"
_RANK = {OK: 0, WARN: 1, BAD: 2}

GB = 1024 ** 3


def _item(key: str, title: str, state: str, detail: str,
          fix: str = "", fix_label: str = "") -> dict:
    """fix: 修复动作，前端按它决定按钮行为
    engine=一键装引擎 / settings=去设置 / recheck=重新检测 / ''=无需操作"""
    return {"id": key, "title": title, "state": state, "detail": detail,
            "fix": fix, "fix_label": fix_label}


# ---------- 各项检查 ----------

def _data_dir(c: dict) -> dict:
    """引擎数据目录：Windows 在不信任的重解析点路径上建不了目录链接（os error 448）

    这是线上真实踩过的坑（客户装完引擎，第一次翻译就报 os error 448 不受信任的装入点）。
    能自动挑到别的目录就当场挑好并把结果写进设置，不给用户留作业。
    """
    import datadir
    before = (c.get("finesub_home") or "").strip()
    home = datadir.ensure(c, None)
    used = home or str(datadir.default_home())
    why = datadir.unusable_reason(Path(used))
    if why:
        return _item("data_dir", "引擎数据目录", BAD,
                     "这个位置不能用（多半是网盘同步或搬家工具留下的目录链接）。"
                     "请在「设置 → 高级选项 → 引擎数据目录」里换一个别的盘上的普通文件夹。",
                     "settings", "去设置")
    if home != before:
        return _item("data_dir", "引擎数据目录", WARN,
                     f"系统默认位置装不了引擎运行环境（os error 448 不受信任的装入点，"
                     f"常见于网盘同步或「搬家」工具留下的目录链接），已自动换到别的目录")
    return _item("data_dir", "引擎数据目录", OK, "已就绪")


def _tools() -> dict:
    """随包分发的 ffmpeg / ffprobe / yt-dlp"""
    import tools
    tools.refresh()
    miss = tools.missing()
    if miss:
        names = "、".join(tools.DESC.get(n, n) for n in miss)
        return _item("tools", "随包运行组件", BAD,
                     f"缺少 {names}。这些程序随安装包自带，多半被杀毒软件隔离了——"
                     f"请在杀毒软件的隔离区恢复，或重新安装本软件。",
                     "recheck", "重新检测")
    return _item("tools", "随包运行组件", OK, "随包的运行组件都已就位")


def _downloader(c: dict, deep: bool = False) -> dict:
    """下载器（yt-dlp）：能不能下 YouTube 全靠它，而它的有效寿命以周计

    yt-dlp 是随包分发的，以前只能靠"发一版新软件"顺带更新它；现在软件启动时会
    自己把它跟上（见 ytdlp.py）。这一项报告的就是"现在装的是哪一版、走的哪个通道"，
    客户说"下不了 YouTube 视频"时，先看这里。
    """
    import ytdlp
    st = ytdlp.state(c)
    if not st.get("path"):
        return _item("downloader", "下载器", BAD,
                     "没找到下载器组件，它随安装包自带，多半被杀毒软件隔离了。"
                     "请在杀毒软件的隔离区恢复，或重新安装本软件。",
                     "recheck", "重新检测")
    ch = "每日构建版" if st["channel"] == "nightly" else "稳定版"
    note = ""
    if st["channel"] == "nightly":
        note = "；之前下载被 YouTube 拦过，已自动切到每日构建版"
    if not st.get("writable"):
        note += "；程序目录写不了，更新会放到用户目录（不影响使用）"
    if not deep:
        return _item("downloader", "下载器", OK, f"{st['version']}（{ch}）{note}")
    remote = ytdlp.remote_tag(st["channel"], c)
    if not remote:
        return _item("downloader", "下载器", WARN,
                     f"{st['version']}（{ch}）{note}。这次没连上 GitHub，查不到有没有新版"
                     f"（不影响已经装好的这一份）。")
    if ytdlp.is_newer(remote, st["version"]):
        return _item("downloader", "下载器", WARN,
                     f"当前 {st['version']}，官方已有 {remote}（{ch}）。"
                     f"下不了 YouTube 视频时先点右边这个按钮。",
                     "downloader", "更新下载器")
    # 稳定版通道下，顺带把每日构建的版本报出来：客户"下不了 YouTube 视频"时，
    # 客服看到 nightly 已经跑在前面，就知道该让他点一次「更新下载器」。
    extra = ""
    if st["channel"] == "stable":
        night = ytdlp.remote_tag(ytdlp.NIGHTLY, c)
        if night and ytdlp.is_newer(night, st["version"]):
            extra = f"；每日构建版已到 {night}（YouTube 变了、稳定版还没跟上时可用）"
    return _item("downloader", "下载器", OK,
                 f"{st['version']}（{ch}），已是最新{note}{extra}")


def _install_dir() -> dict:
    """程序是不是被装进了临时目录

    2026-10-07 真事：安装包的 DefaultDirName 是 `{src}\\StarryVT`（{src} = 安装包自己
    所在的目录），而自动更新是把新安装包下到 `%TEMP%` 再静默跑的 —— 只要注册表里
    "上次安装目录"那条记录不可用，更新就会装进 `%TEMP%\\StarryVT`：界面上的版本号
    死活不涨，而且**下一次更新还会往这儿装**（它会一直错下去）。
    %TEMP% 会被磁盘清理清掉，装在那儿的程序随时可能整个消失，所以这必须报出来。
    """
    import sys
    import tempfile
    import tools
    base = tools.exec_path().parent if tools.is_packaged() else Path(__file__).parent
    try:
        tmp = Path(tempfile.gettempdir()).resolve()
        inside = base.resolve() == tmp or tmp in base.resolve().parents
    except OSError:
        inside = False
    if inside:
        return _item("install_dir", "程序安装位置", BAD,
                     f"程序装在临时目录里（{base}）。这个目录会被系统清理掉，程序随时可能"
                     f"整个消失，而且版本更新会一直装错地方。请重新运行安装包，"
                     f"把安装位置选到一个正式的目录（比如 D:\\StarryVT）。")
    return _item("install_dir", "程序安装位置", OK, str(base))


def _engine(c: dict) -> dict:
    """翻译引擎 finesub"""
    import engine
    st = engine.status(c.get("finesub_exe") or "")
    if st.get("ready"):
        return _item("engine", "翻译引擎", OK, "已就绪")
    if st.get("engine_problem"):
        # 启动器背后的 Python 丢了（2026-10-26 客户报障）：重装引擎本体即可，
        # 运行环境和模型都在，不用重下。
        return _item("engine", "翻译引擎", BAD,
                     st["engine_problem"] + "。点右边按钮重新安装一次引擎本体"
                     "（已下好的运行环境和模型会继续用）。",
                     "engine", "一键安装引擎")
    if st.get("engine_path"):
        # 本体在、运行环境不在：说清楚是「没装完」，别让用户以为要重装一遍
        return _item("engine", "翻译引擎", WARN,
                     "引擎本体已装好，但运行环境还没准备好（多半是下载被限流或中断）。"
                     "点右边按钮继续准备，已下好的部分不会重下。",
                     "engine", "继续准备运行环境")
    return _item("engine", "翻译引擎", BAD,
                 "没有检测到翻译引擎。点右边按钮一键安装，装完就能翻译。",
                 "engine", "一键安装引擎")


def _models(c: dict) -> dict:
    """引擎的运行环境和模型齐不齐 —— 「第一次点开始翻译就卡住不动」的头号原因

    首次翻译前要下两坨大的：运行时（Python 环境 + CUDA 版 torch，约 2.6GB 下载）和
    语音识别模型 whisper（1.62GB）。**这两步在界面上几乎没有进度**（引擎取模型那段
    连一个字都不打印，见 pipeline._ModelWatch 的说明），用户看到的就是"卡住了"。

    与其让人对着不动的界面猜，不如开局就把"还差什么、大概多大"说清楚。
    （新版已经把运行时挪到「一键安装引擎」那一步做了，这里主要是兜老机器。）
    """
    import datadir
    import pipeline
    home = (c.get("finesub_home") or "").strip() or str(datadir.default_home())
    try:
        runtime_ok = (Path(home) / "runtime" / "python" / "Scripts" / "python.exe").is_file()
    except OSError:
        runtime_ok = False
    model_ok = pipeline._hf_whisper_ready(c)
    if runtime_ok and model_ok:
        return _item("models", "引擎运行环境与模型", OK, "运行环境和语音识别模型都已就绪")
    missing = []
    if not runtime_ok:
        missing.append("引擎运行环境（约 2.6 GB）")
    if not model_ok:
        missing.append("语音识别模型（约 1.6 GB）")
    return _item("models", "引擎运行环境与模型", WARN,
                 "还没准备好：" + "、".join(missing) + "。"
                 "第一次翻译前要先下这些，下载期间界面上几乎没有进度（引擎不报），"
                 "这不是卡住了，请耐心等；下好一次以后就不再下了。",
                 "engine", "现在就去准备")


def _keys(c: dict) -> dict:
    """API Key：DeepSeek 是翻译主力（必需），MiMo 给配音用"""
    ds = bool((c.get("deepseek_key") or "").strip())
    mimo = bool((c.get("mimo_key") or "").strip())
    miss = []
    if not ds:
        miss.append("DeepSeek Key（翻译必需）")
    if c.get("do_dub", True) and not mimo:
        miss.append("MiMo Key（配音需要；不想配音可以在界面取消勾选）")
    if miss:
        return _item("keys", "API Key", BAD if not ds else WARN,
                     "还没配置：" + "；".join(miss), "settings", "去设置")
    # 2026-10-21 起 Gemini 不再是翻译兜底（界面里也没有这一项了），平时不用提它；
    # 只有打开着那两个走引擎原生 Gemini 搜索的老开关（视频语境 / 联网查证）时才提示。
    extra = ""
    if (c.get("video_context") or c.get("web_search")) and not (c.get("gemini_key") or "").strip():
        extra = "；Gemini 未配置（视频语境纠错 / 联网查证需要它）"
    return _item("keys", "API Key", OK, f"DeepSeek、MiMo 都已配置{extra}")


def _llm_route(c: dict) -> dict:
    """翻译走哪家大模型

    引擎 finesub 打包的路由表里**只有 Gemini**（国内直连必超时），我们另外写了一份
    模型目录 + 预设把它换成 DeepSeek。这一段确认"翻译真的有模型可用"——客户
    「点开始翻译只出来外文字幕 / 一直失败」的根因就在这儿。见 llmroute.py
    """
    import llmroute
    state, detail = llmroute.status(c)
    return _item("llm_route", "翻译模型", state, detail,
                 "settings" if state != OK else "", "去设置")


_GPU_CACHE: dict = {"at": 0.0, "value": None}


def _gpu_info() -> dict:
    """查 NVIDIA 显卡和显存（nvidia-smi，8 秒超时，结果缓存 60 秒）。

    ⚠️ 别用 WMI 的 AdapterRAM：它是 32 位字段，超过 4 GB 的卡会读成 4 GB 以下。
    nvidia-smi 是唯一稳的取法；没有它本身就说明「没有 NVIDIA 驱动」。
    """
    now = time.time()
    if _GPU_CACHE["value"] is not None and now - _GPU_CACHE["at"] < 60:
        return _GPU_CACHE["value"]
    info = {"name": "", "vram_gb": 0.0, "error": "", "adapters": []}
    # 没有 NVIDIA 时至少要说得出装的是什么卡（AMD / 核显），否则用户以为
    # 软件没认出显卡（2026-10-26 客户报障）。见 tools.display_adapters。
    try:
        import tools
        info["adapters"] = tools.display_adapters()
    except Exception:  # noqa: BLE001
        pass
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        r = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=0x08000000)          # CREATE_NO_WINDOW
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if r.returncode == 0 and lines:
            best_mb, best_name = 0.0, ""
            for line in lines:
                parts = [p.strip() for p in line.split(",")]
                name = parts[0] if parts else ""
                try:
                    mb = float(parts[1]) if len(parts) > 1 else 0.0
                except ValueError:
                    mb = 0.0
                if mb >= best_mb:
                    best_mb, best_name = mb, name
            info.update(name=best_name, vram_gb=round(best_mb / 1024, 1))
        else:
            info["error"] = "nvidia-smi 没有返回显卡信息"
    except Exception as e:  # noqa: BLE001 - 没有显卡/驱动是最常见的情况，不是错误
        info["error"] = type(e).__name__
    _GPU_CACHE.update(at=now, value=info)
    return info


def _hardware(c: dict) -> dict:
    """电脑配置：系统 / 位数 / 显卡（用户 2026-10-21 要求初始化时顺带查）。

    推荐配置（不满足只提示，不拦）：
      · Windows 10 / 11 x64
      · NVIDIA RTX 20 系以上，显存 ≥ 4 GB（没有也能跑，走 CPU，慢很多）
      · 磁盘 ≥ 20 GB（这一项在「磁盘空间」里单独报）
    """
    # ---- 系统 ----
    os_ok = platform.system() == "Windows"
    is64 = sys.maxsize > 2 ** 32
    if os_ok:
        try:
            v = sys.getwindowsversion()
            # Windows 11 的 major/minor 仍然是 10.0，只能靠 build 号区分（22000 起是 11）
            if v.major == 10 and v.build >= 22000:
                os_name = f"Windows 11（build {v.build}）"
            elif v.major == 10:
                os_name = f"Windows 10（build {v.build}）"
            else:
                os_name = f"Windows {v.major}.{v.minor}（build {v.build}）"
            if v.major < 10:
                os_ok = False
        except Exception:  # noqa: BLE001
            os_name = platform.platform()
    else:
        os_name = f"{platform.system()} {platform.release()}"

    if not os_ok:
        return _item("hardware", "电脑配置", BAD,
                     f"系统是 {os_name}。本软件需要 Windows 10 / 11 x64。")
    if not is64:
        return _item("hardware", "电脑配置", BAD,
                     f"{os_name}，但当前是 32 位环境。本软件需要 64 位 Windows。")

    # ---- 显卡 ----
    g = _gpu_info()
    name = g["name"]
    vram = g["vram_gb"]
    if not name:
        adapters = g.get("adapters") or []
        if adapters:
            desc = "、".join(adapters)
            if any("nvidia" in a.lower() for a in adapters):
                detail = (f"{os_name} x64；检测到 {desc}，但 nvidia-smi 不可用"
                          f"（驱动没装好？）。装好 NVIDIA 驱动才能用显卡加速，"
                          f"否则会走 CPU，语音识别会慢很多。")
            else:
                detail = (f"{os_name} x64；检测到 {desc}。引擎只支持 NVIDIA CUDA，"
                          f"这台机器会用 CPU 跑，语音识别会慢很多 —— 能正常出片，"
                          f"不是故障，也没有开关能启用 AMD / 核显加速。")
            return _item("hardware", "电脑配置", WARN, detail)
        return _item("hardware", "电脑配置", WARN,
                     f"{os_name} x64；没检测到 NVIDIA 显卡或驱动。能跑，但会走 CPU，"
                     f"语音识别会慢很多。推荐 NVIDIA RTX 20 系以上、显存 4 GB 以上。")
    upper = name.upper()
    if "RTX" not in upper:
        return _item("hardware", "电脑配置", WARN,
                     f"{os_name} x64；显卡是 {name}。能跑，但推荐 NVIDIA RTX 20 系以上"
                     f"（老卡/专业卡可能更慢，部分新特性用不了）。")
    if vram and vram < 4:
        return _item("hardware", "电脑配置", WARN,
                     f"{os_name} x64；显卡是 {name}，显存 {vram:g} GB。"
                     f"推荐显存 4 GB 以上（显存不够时引擎可能退回 CPU，会明显变慢）。")
    vram_txt = f"，显存 {vram:g} GB" if vram else "（显存读不到）"
    return _item("hardware", "电脑配置", OK,
                 f"{os_name} x64；显卡 {name}{vram_txt}，符合推荐配置。")


def _disk(c: dict) -> dict:
    """磁盘空间：引擎运行环境 + 模型首次要下 5GB 以上

    ⚠️ 阈值必须和 `engine.MIN_FREE_GB` / `COMFY_FREE_GB` **对得上**（2026-10-16 对齐）：
      那边是**硬拦**（不够就一个字都不下），这里只是自检报告里提前报一句。
      两边不一致的话，用户会看到「报告说没问题、点下去却被拦住」这种自相矛盾。
    """
    import datadir
    import engine
    home = (c.get("finesub_home") or "").strip() or str(datadir.default_home())
    anchor = Path(home).anchor or home
    try:
        free = shutil.disk_usage(anchor).free
    except OSError:
        return _item("disk", "磁盘空间", WARN, f"读不到 {anchor} 的剩余空间（不影响使用）")
    gb = free / GB
    if gb < engine.MIN_FREE_GB:
        return _item("disk", "磁盘空间", BAD,
                     f"{anchor} 只剩 {gb:.1f} GB，装引擎至少要 {engine.MIN_FREE_GB:.0f} GB —— "
                     f"点「一键初始化」会被拦下来（盘写满会连带整台机器出问题，所以不硬着头皮下）。"
                     f"先腾出空间，或在设置里把「引擎数据目录」换到别的盘。建议至少留 20 GB。",
                     "settings", "去设置")
    if gb < engine.COMFY_FREE_GB:
        return _item("disk", "磁盘空间", WARN,
                     f"{anchor} 只剩 {gb:.1f} GB。装引擎（约 5 GB）够用，"
                     f"但第一次翻译还要再下约 6 GB 的语音模型，建议至少留 20 GB。"
                     f"想换盘可以在设置里改「引擎数据目录」。",
                     "settings", "去设置")
    return _item("disk", "磁盘空间", OK, f"{anchor} 剩余 {gb:.1f} GB（建议 ≥ 20 GB）")


def _output(c: dict) -> dict:
    """输出目录能不能写（装在只读位置 / 无权限的盘会失败）

    本地输入时是「视频旁边的 output\\」（pipeline.resolve_output_base），
    写不进去它会自动退回程序目录 —— 所以这里报的就是真正会落盘的那个路径。
    """
    from pipeline import resolve_output_base
    out = resolve_output_base(c)
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".starryvt-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except Exception as e:  # noqa: BLE001
        return _item("output", "输出目录", BAD,
                     f"{out} 写不进去（{type(e).__name__}）——请在设置里换一个目录。",
                     "settings", "去设置")
    return _item("output", "输出目录", OK, str(out))


def _proxy(c: dict) -> dict:
    """代理：只有下载 YouTube 和 Gemini 增强才需要"""
    import mirrors
    configured = (c.get("proxy") or "").strip()
    proxy = mirrors.effective_proxy(c)
    if proxy:
        if configured and proxy == configured:
            return _item("proxy", "网络代理", OK, proxy)
        return _item("proxy", "网络代理", OK, f"自动检测到本机代理：{proxy}")
    if configured:
        return _item("proxy", "网络代理", WARN,
                     f"你填的代理 {configured} 连不上，也没自动检测到本机代理。"
                     f"没开代理软件的话，去设置里把这一栏清空。",
                     "settings", "去设置")
    if (c.get("source") or "").strip().lower().startswith("http"):
        return _item("proxy", "网络代理", WARN,
                     "要下载在线视频通常需要代理；没有代理的话，请改用「本地文件」模式。",
                     "settings", "去设置")
    return _item("proxy", "网络代理", OK, "未设置（翻译本地文件不需要）")


def _github(c: dict, deep: bool = False) -> dict:
    """外网依赖：有几样东西只放在 GitHub 上（引擎自带的 ffmpeg / 人声分离模型 /
    那个只能从 GitHub 下的 Python 包）。没代理的用户靠「本机分发 + 国内加速镜像」，
    这里报告的就是这套兜底机制当前的状态。"""
    import mirrors
    import wheels
    title = "外网依赖"
    configured = (c.get("proxy") or "").strip()
    proxy = mirrors.effective_proxy(c)
    if proxy:
        if configured and proxy == configured:
            return _item("github", title, OK, "已配网络代理，外网依赖直接走官方源")
        return _item("github", title, OK, f"自动检测到本机代理 {proxy}，外网依赖直接走官方源")
    # ⚠️ 这一行以前只看「填没填」：填了个连不上的地址也报「已配网络代理，直接走官方源」。
    #    客户拿着这份报告以为外网没问题，其实是死端口，什么都下不来（2026-10-15 实例）。
    if configured:
        return _item("github", title, WARN,
                     f"你填的代理 {configured} 连不上，也没自动检测到本机代理，"
                     f"已改用国内加速镜像。本机没开代理软件的话，去设置里把这一栏清空。",
                     "settings", "去设置")
    if not mirrors.enabled(c):
        return _item("github", title, WARN,
                     "GitHub 加速镜像已关闭。没有代理的机器可能装不上引擎组件"
                     "（引擎自带的 ffmpeg、人声分离模型等）。",
                     "settings", "去设置")
    if deep:
        prefix = mirrors.pick(c, None, force=True)
        if prefix:
            return _item("github", title, OK, f"加速镜像可用：{prefix}")
        return _item("github", title, WARN,
                     "候选加速镜像都连不上——没有代理时引擎组件可能下载失败。"
                     "可以稍后点「重新检测」，或在设置里自己填一个镜像地址。",
                     "recheck", "重新检测")
    if any("本机分发" in line for line in wheels.describe()):
        return _item("github", title, OK, "引擎必需的 GitHub 组件已随软件分发，无需联网")
    return _item("github", title, OK, "加速镜像自动选择（开工时检测）")


def _net(c: dict) -> dict:
    """联网检查：翻译主力是 DeepSeek，先确认连得上（只在 deep=True 时跑）"""
    title = "网络连通（DeepSeek）"
    key = (c.get("deepseek_key") or "").strip()
    if not key:
        return _item("net", title, WARN, "还没填 DeepSeek Key，跳过联网检查", "settings", "去设置")
    import mirrors
    # 有可用代理就走，没有就强制直连（系统代理指向死端口时别被它带崩）
    opener = mirrors.opener(c)
    req = urllib.request.Request("https://api.deepseek.com/models",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with opener.open(req, timeout=10) as r:
            return _item("net", title, OK, f"连接正常（HTTP {r.status}）")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return _item("net", title, BAD, f"Key 无效或被拒（HTTP {e.code}），检查是否填错",
                         "settings", "去设置")
        return _item("net", title, WARN, f"服务返回 HTTP {e.code}（可能是额度或限流）")
    except Exception as e:  # noqa: BLE001
        return _item("net", title, BAD,
                     f"连不上（{type(e).__name__}）——检查网络；下载在线视频还需要代理。",
                     "settings", "去设置")


# ---------- 汇总 ----------

def check(params: dict | None = None, deep: bool = False) -> dict:
    """跑一遍自检，返回 {state, summary, bad, warn, items}"""
    import settings as cfg
    c = dict(cfg.load())
    c.update(params or {})

    items = [_hardware(c), _data_dir(c), _install_dir(), _tools(), _downloader(c, deep),
             _engine(c), _models(c), _keys(c), _llm_route(c), _disk(c), _output(c),
             _proxy(c), _github(c, deep)]
    if deep:
        items.append(_net(c))

    bad = sum(1 for i in items if i["state"] == BAD)
    warn = sum(1 for i in items if i["state"] == WARN)
    if bad:
        summary = f"{bad} 项需要处理" + (f"，另有 {warn} 项提示" if warn else "")
    elif warn:
        summary = f"可以用（{warn} 项提示，已尽量自动处理）"
    else:
        summary = "一切正常"
    return {"state": BAD if bad else (WARN if warn else OK),
            "summary": summary, "bad": bad, "warn": warn, "items": items}
