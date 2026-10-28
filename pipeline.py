#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""流水线编排：下载(可选) -> finesub 翻译 -> MiMo 配音 -> 烧字幕 -> 成片

    from pipeline import run_pipeline
    result = run_pipeline(params, job, report)

params 见 settings.DEFAULTS；report(stage, percent, line) 汇报进度与日志。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job import Job, Cancelled
import datadir
import dubber
import en_translate
import engine
import engine_mirror
import llmroute
import mirrors
import srt2ass
import subtitle_fonts
import tools
import wheels
import ytdlp
import ytsub


def _writable(cand: Path) -> bool:
    """建目录 + 写个小文件试试，能不能真的落盘（只读盘 / 没权限会失败）"""
    try:
        cand.mkdir(parents=True, exist_ok=True)
        probe = cand / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _default_output_dir() -> Path:
    """兜底输出目录：程序目录下的 output/（装在只读位置时回退到「视频库\\StarryVT」）"""
    base = tools.exec_path().parent if tools.is_packaged() else Path(__file__).parent
    cand = base / "output"
    return cand if _writable(cand) else Path.home() / "Videos" / "StarryVT"


# 成品整条路径的长度上限。Windows 的 MAX_PATH 是 260，超过之后不少播放器 /
# 剪辑软件（实测 PotPlayer）会一直卡在「即将播放」打不开 —— 留 20 个字符余量。
MAX_OUT_PATH = 240


def _fit_stem(stem: str, budget: int) -> str:
    """把输出文件名截到 budget 个字符（只影响输出目录里的副本，引擎工作目录不变）

    头和尾各留一段，中间用 ~ 连接 —— 长标题往往两头才是能认出来的部分
    （比如「Creating a Subsurface ... Substance Designer」）。
    """
    budget = max(8, budget)
    if len(stem) <= budget:
        return stem
    if budget <= 10:
        return stem[:budget - 1].rstrip(" .") + "~"
    head_len = max(4, int(budget * 0.6) - 1)
    tail_len = budget - head_len - 1
    head = stem[:head_len].rstrip(" .")
    tail = stem[-tail_len:].lstrip(" .")
    if not head:
        return tail
    if not tail:
        return head + "~"
    return f"{head}~{tail}"


def _source_folder(src: str) -> Path | None:
    """本地输入（视频文件 / 文件夹）所在的那一层；链接、空、没这个路径都返回 None

    路径必须先真实存在才认 —— 否则一个打错的路径会凭空猜出一个「视频旁边的 output」
    来（还可能顺手建出来），不如老老实实走兜底目录。
    """
    src = (src or "").strip()
    if not src or re.match(r"^https?://", src, re.I):
        return None
    p = Path(src)
    if p.is_dir():
        return p
    if p.is_file():
        return p.parent
    return None


def resolve_output_base(params: dict, report=None, create: bool = True) -> Path:
    """这次任务的输出根目录 —— 默认就落在「原视频旁边」

    本地视频/文件夹：<所在文件夹>\\output\\（那地方写不进去就退回程序目录，并说一声）；
    链接 / YouTube 合集是下下来才知道，没有本地来源，也走程序目录。
    设置里填了「输出目录」就一律以设置为准，一个字节都不猜。
    """
    cfg = (params.get("output_dir") or "").strip()
    if cfg:
        return Path(cfg)
    folder = _source_folder(params.get("source"))
    if folder is not None:
        cand = folder / "output"
        if not create:
            return cand
        if _writable(cand):
            return cand
        if report:
            report("download", None,
                   f"提示：{cand} 写不进去（磁盘只读或没有权限），"
                   f"这次的成品改存到 {_default_output_dir()}")
    return _default_output_dir()

# 各阶段在总进度条上的区间（%）
SPAN = {"download": (0, 8), "translate": (8, 55), "dub": (55, 85), "burn": (85, 100)}

#: 快速模式：不分离 + Google 机翻 + 微软 edge-tts（2026-10-28 用户定）
_GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"


def _is_fast(params: dict) -> bool:
    """设置里的「翻译模式」是不是快速模式"""
    return str(params.get("mode") or "standard").strip().lower() == "fast"


def _google_one(text: str, timeout: float = 12.0) -> str:
    """单条 Google 机翻（gtx 非官方接口）。失败返回空串。"""
    import urllib.parse
    import urllib.request
    q = urllib.parse.quote(text.replace("\n", " "))
    url = f"{_GOOGLE_URL}?client=gtx&sl=auto&tl=zh-CN&dt=t&q={q}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except Exception:  # noqa: BLE001 - 重试几次就放弃，原文兜底
            time.sleep(0.4 * (attempt + 1))
    return ""


def _fast_translate(video: Path, params: dict, job: Job, report) -> Path:
    """快速模式翻译：引擎只跑 ASR（--no-separate），译文走 Google 机翻。"""
    # 用户导入的字幕/视频旁的同名字幕优先；放好了引擎就会跳过识别
    if not _prepare_user_subtitle(video, params, job, report):
        params.pop("subtitle_file", None)
    raw = _translate_once(video, params, job, report, stage="raw-srt", no_separate=True)
    cues = dubber.parse_srt(raw)
    if not cues:
        raise RuntimeError("快速模式：识别结果为空，没法翻译")
    texts = [c[2] for c in cues]
    report("translate", _map("translate", 0.45),
           f"快速模式：Google 机翻 {len(texts)} 条（不做 AI 纠错）…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        fut = [pool.submit(_google_one, t) for t in texts]
    zh: list[str] = []
    miss = 0
    for i, f in enumerate(fut):
        job.check()
        out = f.result()
        if not out:
            miss += 1
            out = texts[i]
        zh.append(out)
    out = raw.parent / f"{video.stem}.srt"
    out.write_text(ytsub.to_srt([(s, e, t) for (s, e, _old), t in zip(cues, zh)]),
                   encoding="utf-8")
    if miss:
        report("translate", None, f"（{miss} 条机翻没返回，保留原文）")
    report("translate", _map("translate", 1.0),
           "快速模式翻译完成（Google 机翻，术语可能不如 AI 纠错准）")
    return out


#: 纠错翻译的窗口并发数 / 触发时长（秒）
#: 2026-10-27 实测：≥2 个窗口（约 15 分钟素材）时 4 路并发把 LLM 阶段压缩到
#: 原来的三分之一左右；单窗口时并发反而慢 18%，所以按素材时长闸住。
PARALLEL_WINDOWS = 4
PARALLEL_MIN_SEC = 15 * 60


def _parallel_windows_for(seconds: float) -> int:
    """长视频才开窗口并发（短视频并发反而慢，见 PARALLEL_WINDOWS 的注释）"""
    try:
        return PARALLEL_WINDOWS if float(seconds) >= PARALLEL_MIN_SEC else 0
    except (TypeError, ValueError):
        return 0


def _llm_parallel_args(seconds: float) -> list[str]:
    """引擎的并发开关。**两个参数必须一起给**：只有 --llm-parallel-windows
    时引擎仍走串行驱动（continuity 默认 serial，见引擎 pipeline.py 的
    choices=["serial", "parallel"]），等于白开。

    代价：parallel 不共享窗口之间链式的上下文（引擎文档
    docs/llm_harness_behavior.md）。2026-10-27 在合成长片上没测到明显变差，
    但真实素材没有对照过 —— 所以只在 ≥15 分钟的长片上开。
    """
    n = _parallel_windows_for(seconds)
    return (["--llm-continuity", "parallel", "--llm-parallel-windows", str(n)]
            if n else [])


def _dub_eta_text(done: int, total: int, started: float,
                  now: float | None = None) -> str:
    """配音进度文本（界面横幅用）：已合成 X/Y 句 + 按当前速度估算的剩余时间。

    2026-10-28 用户反馈「一条长视频快 1 小时了」—— 配音是大头，
    但界面只有一个百分比，看不出还剩多久。这里给一句人话。
    """
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    base = f"配音合成 {done}/{total}"
    if done <= 0 or done >= total:
        return base
    now = time.monotonic() if now is None else now
    remain = max(0.0, (now - started) / done * (total - done))
    if remain >= 60:
        return f"{base} · 预计剩余 {int(round(remain / 60))} 分钟"
    return f"{base} · 预计剩余 {int(round(remain))} 秒"

_RE_STAGE = re.compile(r"^\[(\d)/4\]\s*\S+\s*(?:(\d+)%\s*·\s*(\d+)/(\d+))?")
_RE_SRT_PATH = re.compile(r"([A-Za-z]:\\[^\r\n]*?\.srt)")
_RE_YTDL = re.compile(r"\[download\]\s+([\d.]+)%")
_RE_FFPROG = re.compile(r"out_time_ms=(\d+)")

# 看起来是"临时性"的失败（网络抖动/服务限流），值得自动重试。
#
# ⚠️⚠️ 判的时候**不能拿裸子串进去搜**：`"ssl" in blob` 会被引擎堆栈里的
#     `transcode_to_lossless_audio` 命中 —— 于是一个**坏掉的本地视频**被当成"网络抖动"，
#     连着重试 3 轮（每轮之间还等 10 秒），最后报的还是"检测到网络/服务临时故障
#     （SSL/连接/限流）"，把用户往网络问题上带（2026-10-14 实测）。
#     统一走 _looks_transient()：只认"从词首开始"的命中，数字类的再要求前后不是数字。
_TRANSIENT = ("ssl", "unexpected_eof", "connecterror", "connection", "timeout", "timed out",
              "503", "502", "429", "econnreset", "remote end closed", "temporarily")

# 本地文件本身坏了 / 根本不是视频时 ffmpeg 的原话。这种别重试（重试多少次都一样），
# 也别报成网络问题 —— 直接告诉用户换个文件。
_BROKEN_MEDIA = ("moov atom not found", "invalid data found when processing input",
                 "misdetection possible", "error opening input file")


def _looks_transient(blob: str) -> bool:
    """blob 要传**小写**过的引擎输出"""
    for k in _TRANSIENT:
        if k.isdigit():
            if re.search(rf"(?<!\d){k}(?!\d)", blob):
                return True
        elif re.search(rf"(?<![a-z0-9_]){re.escape(k)}", blob):
            return True
    return False


class TransientError(RuntimeError):
    """临时性失败（网络/服务），可整段重试"""


class GithubBlocked(RuntimeError):
    """引擎要去 GitHub 下依赖但下不动（无代理的国内用户），换镜像/本机分发后可重试"""


# 引擎报的「GitHub 下不动」特征（见 wheels / mirrors 说明）
_GITHUB_SIGNS = ("failed to download", "tcp connect error", "deadline has elapsed",
                 "failed to fetch", "request failed after")


def _looks_github_blocked(blob: str) -> bool:
    """引擎是不是卡在 GitHub 上下不动（无代理用户的典型失败）

    只看 blob 里有没有 GitHub 地址 + 连接失败的痕迹。同一次失败的 traceback
    里两样都在，所以这个判断很稳；反过来，普通网络抖动不会有 github.com。
    """
    b = blob.lower()
    return "github.com" in b and any(k in b for k in _GITHUB_SIGNS)


def _llm_unreachable_hint(blob: str, params: dict) -> str:
    """引擎连不上翻译用的 LLM 时给一句人话；不该管的情况返回空串

    `WinError 10060` 是"压根连不上"，不是"网络抖动"：国内直连 Google 必失败。
    原来它归在 _TRANSIENT 里，用户要干等三轮重试（客户实测一轮十几分钟），
    最后拿到的还是一句"网络/服务临时故障"。这里只处理**没配代理**的情况；
    配了代理就可能是真抖动，交给原来的重试逻辑。
    """
    if not any(k in blob for k in ("10060", "connecttimeout", "connect timeout")):
        return ""
    if mirrors.effective_proxy(params):
        return ""
    if params.get("video_context") or params.get("web_search"):
        return ("翻译引擎要联网做「视频语境纠错 / 联网查证」，但这台机器连不上 Google"
                "（连接超时）。请在「设置 → 高级选项 → 网络代理」填上代理，"
                "或者把这两个增强关掉再试——只用 DeepSeek 翻译是不需要代理的。")
    if params.get("gemini_key") and not params.get("deepseek_key"):
        return ("翻译引擎正在用 Gemini 翻译，但这台机器连不上 Google（连接超时）。"
                "国内用 Gemini 需要代理；换成「设置」里的 DeepSeek key 就不需要代理。")
    return ""


def _llm_missing_hint(blob: str, params: dict) -> str:
    """引擎说「选不出可用的大模型」时给一句人话（多半是 key 没填对）

    引擎的原话是 `ProviderUnavailableError: Provider XX is disabled or has no
    selected API key. [chain exhausted: ...]`——客户只会看到最后那句
    「finesub 翻译失败（看日志…）」，不知道要干什么。这里翻成能照做的中文。
    """
    if "providerunavailable" not in blob and "chain exhausted" not in blob:
        return ""
    if not llmroute.targets(params):
        return ("还没配置翻译用的 API Key。请打开右上角「设置」填入 DeepSeek Key"
                "（设置里有申请入口）——翻译本地文件不需要代理，填完就能用。")
    return ("翻译引擎找不到可用的大模型：多半是 Key 填错/额度用完，或这个模型不支持"
            "当前开关组合。请到「设置」核对 DeepSeek Key，再点顶栏「环境自检」看一眼。")


def _engine_crashed_hint(code: int, tail: list[str]) -> str:
    """引擎「一声不吭就没了」时给一句能照做的话

    客户 2026-10-08 实测：界面停在 `[2/4] 语音识别` 26 分钟，然后子进程退出，
    而 stdout 里**一个字都没有** —— 没有 Python 堆栈，就说明不是普通异常，是硬死
    （原生崩溃 / 被外力结束 / 内存耗尽）。这时候该告诉用户「关掉重开再来一次」，
    以及「老是这样就把诊断报告发我们」，而不是笼统的一句"看日志"。
    """
    crashed = code < 0 or code >= 0x80000000      # Windows 异常码（0xC0000005 等）
    quiet = sum(1 for line in tail if line.strip()) <= 2
    if not (crashed or quiet):
        return ""
    return ("翻译引擎中途异常退出，没有留下任何错误信息。\n"
            "常见原因是内存不够、被杀毒软件拦截、或显卡驱动异常。\n"
            "请把软件完全关掉再打开、重试一次；如果每次都在同一步停住，"
            "请点「设置 → 导出诊断报告」发给我们，报告里带着引擎自己的详细运行记录。")


def _engine_broken_hint(code: int, blob: str) -> str:
    """引擎启动器说「No Python at …」：uv 托管的 Python 没了/访问不了

    2026-10-26 客户报障：`finesub.exe` 只是启动器，背后的 Python 丢了时只打一行
    `No Python at '…'`、退出码 103。以前被 `_engine_crashed_hint` 的「没有堆栈 =
    硬死」规则抓走，界面提示「内存不够 / 杀毒软件 / 显卡驱动」—— 客户照做一整天
    也好不了，因为缺的是一个解释器。
    """
    low = (blob or "").lower()
    if "no python at" in low:
        return ("翻译引擎的 Python 运行环境丢了（不是内存、杀毒软件或显卡的问题）。\n"
                "请到「设置 → 翻译引擎」点「一键安装引擎」重新安装一次；"
                "只重装引擎本体，已经下好的运行环境和模型不会重下。")
    if code == 103 and not low.strip():
        return ("翻译引擎没能启动（退出码 103，通常是它的 Python 运行环境丢了）。\n"
                "请到「设置 → 翻译引擎」点「一键安装引擎」重新安装一次。")
    return ""


def _busy_or_denied_hint(blob: str) -> str:
    """引擎数据目录「被别的进程占着」或「写不进去」时给一句人话

    两种都不该按"网络抖动"白重试三轮（每轮十几分钟）。都是 2026-10-08 客户实测到的：

    · **被别的进程占着**——关软件时没杀干净，上一轮的 `uv.exe` 还在下 torch 并握着
      缓存锁，新的进程干等 300 秒拿不到锁。原话：
      `Failed to acquire lock on the distribution cache ... is another uv process running?`
      （这个 bug 我们已经修了：见 job.guard() 和 job.kill_tree()。但老版本用户、
      以及被 taskkill 之类绕过的场合仍会碰到，所以提示要留着。）
    · **写不进去**——`os error 5 拒绝访问`，出现在 uv 缓存目录的改名上，
      典型原因是杀毒软件实时防护锁文件。
    """
    lock_signs = ("failed to acquire lock", "could not acquire lock",
                  "another uv process running", "when waiting for lock")
    if any(k in blob for k in lock_signs):
        return ("引擎数据目录正被另一个进程占用（多半是上一次关闭软件时留下的后台进程）。\n"
                "请打开「任务管理器」，把所有 finesub.exe、uv.exe 以及路径里带 FineSub 的 "
                "python.exe 结束掉，然后重新开始。\n"
                "（新版已经会在退出时自动清理这些进程；如果还反复出现，请把诊断报告发给我们。）")
    deny_signs = ("os error 5", "拒绝访问", "access is denied", "permission denied")
    if any(k in blob for k in deny_signs):
        return ("引擎数据目录写不进去（系统报「拒绝访问」）——多半是杀毒软件/安全软件"
                "把文件锁了。\n"
                "请把这两个目录加入杀毒软件的信任（白名单）后重新开始：\n"
                f"  · 引擎数据目录（设置 → 高级选项 → FINESUB_HOME）\n"
                f"  · 软件自己的目录")
    return ""


def _map(stage: str, frac: float) -> int:
    lo, hi = SPAN[stage]
    return int(lo + (hi - lo) * max(0.0, min(1.0, frac)))


def _subprocess_env(params: dict) -> dict:
    """构造子进程环境：把外网流量交给本机代理（Clash 等会按规则分流，国内站点仍直连）

    翻译（Google/Gemini）与 YouTube 下载在国内必须走代理；本机回环始终直连。
    """
    env = dict(os.environ)
    proxy = mirrors.effective_proxy(params)
    # ⚠️ 必须探通才往下传：填了个连不上的地址时，yt-dlp 连 B 站都会失败
    #    （B 站本来压根不需要代理），用户会以为「在线翻译坏了」。见 mirrors.proxy_ok。
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    else:
        # 没有可用代理：清掉继承来的代理变量并强制直连。否则子进程会去读 Windows
        # 系统代理，代理软件关掉后那个端口是死的 → WinError 10061。
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = env["no_proxy"] = "*"
    return env


def probe_duration(path: Path) -> float:
    """取视频时长（只用于进度百分比；拿不到就返回 0，不影响出片）"""
    from job import no_window_kwargs
    exe = tools.find("ffprobe")
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                           capture_output=True, text=True, **no_window_kwargs())
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001 - 探测失败不该中断任务
        return 0.0


# ---------- 各阶段 ----------

def _friendly_download_error(blob: str, retried: bool = False) -> str:
    """把 yt-dlp 的原始报错翻译成用户能照做的提示

    retried=True 时说明调用方已经替用户自动重试过了——提示里要讲出来，
    否则用户会以为再点一次「开始」就好了。
    """
    b = blob.lower()
    if "403" in b or "forbidden" in b or "unable to download video data" in b:
        done = "软件已自动重试、并尝试升级过下载器，" if retried else ""
        return (f"YouTube 反爬拦截（HTTP 403）——{done}仍未通过。"
                "常见原因是当前网络出口被 YouTube 临时限制：过几分钟再试一次往往就好了；"
                "也可以先手动下载视频，再用「本地文件」模式。")
    if "sign in to confirm" in b or "not a bot" in b:
        return "YouTube 要求登录验证（人机校验）——建议先手动下载视频，再用「本地文件」模式。"
    if "private video" in b or "video unavailable" in b or "removed" in b or "not available" in b:
        return "视频不可用（可能已删除 / 私享 / 有地区限制）。"
    if "ssl" in b or "timed out" in b or "timed out" in b or "connection" in b or "temporary failure" in b:
        return "网络连接失败——检查设置里的「网络代理」是否可用（访问 YouTube 通常需要代理）。"
    if "unsupported url" in b or "is not a valid url" in b:
        return "链接格式不对——请粘贴完整的视频链接。"
    return "yt-dlp 下载失败（详见上方日志）。"


# 值得重试的下载失败：YouTube 风控 / 网络抖动。
# "视频不存在""链接写错"这类重试多少次都一样，早点报错，别让用户干等。
_RETRY_DL = ("403", "forbidden", "unable to download video data", "unable to download webpage",
             "sign in to confirm", "not a bot", "429", "too many requests", "http error 5",
             "connection", "timed out", "timeout", "temporarily", "reset by peer",
             "eof occurred", "ssl")

_DL_TRIES = 3


def _worth_retry(blob: str) -> bool:
    b = blob.lower()
    return any(k in b for k in _RETRY_DL)


def _ytdlp_args(url: str, out_dir: Path, params: dict | None = None) -> list[str]:
    """下载视频用的固定参数（默认 720p mp4，顺带下封面 + 字幕）"""
    params = params or {}
    cmd = [tools.path("yt-dlp")]
    if shutil.which("node"):                      # 有 node 就指定，用于解 YouTube 的 JS 挑战
        cmd += ["--js-runtimes", "node"]
    # 格式选择（按喜好依次退让）：
    #   ① YouTube 那种 mp4+m4a 分开的 ② B 站那种 DASH（扩展名可能是 m4s，别挑扩展名）
    #   ③ 已经是合体的单文件 ④ 最后交给 yt-dlp 自己挑最好的
    # —— 原来只写①/③，B 站上①匹配不上、③常常没有 720p，会直接报"没有合适的格式"
    # ⚠️ 上限从 720 提到 1080（2026-10-28 用户报「成片比原片糊」）：源视频有
    #    1080p 时，720p 的选择器在**下载阶段**就把像素砍了一半，后面烧字幕再怎么
    #    编码也补不回来。实测同一条片子 1080p AV1 只比 720p 大 17 MB，不心疼。
    cmd += ["--encoding", "utf-8",           # 中文站的标题/进度按 UTF-8 出来（默认跟系统代码页，会是乱码）
            # ★ 把**最终文件的真实路径**写下来，别再靠「目录里最新的 mp4」猜
            #   （那个目录是整批共用的 `_下载中`，同名视频 yt-dlp 会跳过并返回 0，
            #    猜出来的可能是上一条视频的文件 —— 2026-10-15 查出来的）。
            #   --print-to-file 和一个正常的下载不冲突；但 --print* 系列默认会顺手把
            #   下载变成「只模拟」，所以必须一起给 --no-simulate。
            "--no-simulate",
            "--print-to-file", "after_move:filepath", str(out_dir / ".last-file"),
            "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba"
                   "/b[height<=1080]/b",
            "--merge-output-format", "mp4", "--newline",
            # 连接层的重试交给 yt-dlp 自己做：以前只有外层「整条命令重跑」，
            # 2026-10-28 实测音频轨连挂两次，每次都要等它超时，7 分钟下载里
            # 有 3.5 分钟耗在重跑上（分片会续传，但等待是实打实的）。
            "--retries", "10", "--fragment-retries", "10",
            "--write-thumbnail", "--convert-thumbnails", "jpg",   # 同时下载封面（转 jpg）
            "-o", str(out_dir / "%(title).80s.%(ext)s"), url]
    return cmd


def fetch_subs(url: str, out_dir: Path, params: dict, report) -> int:
    """单独把创作者字幕拉下来（best-effort，返回拿到几个 .vtt）

    ⚠ **必须和视频下载分开跑**。字幕接口会限流，而 yt-dlp 遇到它会给非零退出码：

        [info] Downloading 1 format(s): 398+140-20      ← 视频已经下好了
        ERROR: Unable to download video subtitles for 'en': HTTP Error 429

    2026-10-13 实测。字幕只是锦上添花（有它就能跳过语音识别），
    绝不能让它的一次 429 把整条视频下载判死——所以放这儿、失败只记一行日志。
    """
    subs = (params.get("yt_subs") or "manual").lower()
    if subs == "off":
        return 0
    exe = tools.path("yt-dlp")
    if not exe:
        return 0
    lang = (params.get("source_lang") or "en").split("-")[0]
    cmd = [exe]
    if shutil.which("node"):                      # 解 YouTube 的 JS 挑战
        cmd += ["--js-runtimes", "node"]
    cmd += ["--encoding", "utf-8",
            "--skip-download", "--no-warnings",   # 只要字幕，别再下一遍视频
            "--write-subs", "--sub-format", "vtt",
            "--sub-langs", f"{lang},{lang}-orig,{lang}-en"]
    if subs == "auto":
        cmd += ["--write-auto-subs"]              # 没有人工轨时退到自动轨
    cmd += ["-o", str(out_dir / "%(title).80s.%(ext)s"), url]
    try:
        from job import no_window_kwargs
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, cwd=str(out_dir),
                           env=_subprocess_env(params), **no_window_kwargs())
    except Exception as e:  # noqa: BLE001
        report("download", None, f"（YouTube 字幕没取到，照常用语音识别：{e}）")
        return 0
    try:
        n = len(list(out_dir.glob("*.vtt")))
    except OSError:
        n = 0
    if n:
        report("download", None, f"已取到 YouTube 字幕（{n} 个文件）")
    else:
        report("download", None, "（YouTube 没有可用字幕，照常用语音识别）")
    return n


def probe_info(url: str, params: dict, timeout: float = 30.0) -> dict:
    """只取标题和封面，不下载视频（给「按标题自动填背景信息」和封面图用）

    合集链接只取第一集——够用来判断讲的是哪款软件了，别为了一句提示去翻整个合集。
    封面用视频 id 直接拼 i.ytimg.com 的地址，省一次网络请求。
    """
    exe = tools.path("yt-dlp")
    if not exe:
        return {}
    cmd = [exe, "--encoding", "utf-8", "--skip-download", "--no-warnings", "--playlist-items", "1",
           "--print", "%(title)s\t%(id)s\t%(uploader)s\t%(duration_string)s\t%(thumbnail)s",
           "--no-download-archive"]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    cmd.append(url)
    r = None
    # 取元数据偶尔会抽风（B 站实测会回一句 "No video formats found"），隔一下重试一次
    for attempt in (0, 1):
        try:
            from job import no_window_kwargs
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout,
                               env=_subprocess_env(params), **no_window_kwargs())
        except Exception:  # noqa: BLE001
            r = None
        if r is not None and r.returncode == 0:
            break
        if attempt == 0:
            time.sleep(0.8)
    if r is None or r.returncode != 0:
        return {}
    line = next((ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()), "")
    # 标题里可能有制表符，所以固定从右边取最后四个字段
    parts = line.rsplit("\t", 4) if "\t" in line else [line]
    while len(parts) < 5:
        parts.append("")
    title, vid, uploader, dur, pic = (p.strip() for p in parts[:5])
    out = {"title": title[:200], "uploader": uploader[:80], "duration": dur[:16]}
    # 真封面地址优先（非 YouTube 的站点也有封面）；没有就按视频 id 拼 YouTube 的
    if pic and pic not in ("NA", "none", "None"):
        out["thumb"] = pic
    elif vid:
        out["thumb"] = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    return out


def probe_title(url: str, params: dict, timeout: float = 30.0) -> str:
    """只要标题（老接口，内部走 probe_info）"""
    return probe_info(url, params, timeout).get("title", "")


def _ytdlp_once(cmd: list[str], out_dir: Path, params: dict, job: Job, report) -> tuple[int, str]:
    """跑一次 yt-dlp，边跑边把日志转成进度；返回 (退出码, 末尾 40 行日志)"""
    # 清掉上一次留下的路径记录：这次没下成的话，绝不能拿上次那条去交差
    try: (out_dir / ".last-file").unlink(missing_ok=True)
    except Exception: pass
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding="utf-8", errors="replace", cwd=str(out_dir),
                  env=_subprocess_env(params))
    tail: list[str] = []
    for line in p.stdout:
        line = line.rstrip()
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        m = _RE_YTDL.search(line)
        if m:
            report("download", _map("download", float(m.group(1)) / 100), line)
        elif line.startswith("[download] Destination:"):
            name = Path(line.split("Destination:", 1)[1].strip()).name
            report("download", _map("download", 0.02), "开始下载: " + name)
        elif line.startswith("[Merger]") or "has already been downloaded" in line:
            report("download", _map("download", 0.98), line)
        elif line:
            report("download", None, line)  # 纯日志行
    return p.wait(), "\n".join(tail)


def download(url: str, out_dir: Path, params: dict, job: Job, report) -> Path:
    """yt-dlp 下载（默认 720p mp4），返回下载文件路径

    **为什么要有重试**：YouTube 的 403 绝大多数是**一时风控**，不是下载器过时。
    2026-10-07 实测：用户撞 403 的那一刻失败，隔一会儿同一条命令、同一个 yt-dlp
    版本原样再跑就成功了。而当时软件撞一次就直接说"下载器过时了，去官网下载最新版
    覆盖安装"——用户白重装一遍软件，问题其实早就自己好了。

    所以现在：先原样重跑（多数情况这一次就过了）；还不行就把下载器升到 nightly
    每日构建再试（见 ytdlp.py）；第三次是最后机会。三条都试过才把错误抛给用户。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _ytdlp_args(url, out_dir, params)
    blob, code = "", 0
    for attempt in range(1, _DL_TRIES + 1):
        code, blob = _ytdlp_once(cmd, out_dir, params, job, report)
        if code == 0:
            break
        job.check()                       # 用户点了取消就立刻走，别在这儿睡
        if attempt >= _DL_TRIES or not _worth_retry(blob):
            raise RuntimeError(_friendly_download_error(blob, retried=attempt > 1))
        if attempt == 1:
            report("download", None, "下载被 YouTube 挡了一下，2 秒后自动重试…")
            time.sleep(2.5)
        else:
            # 还不行就把下载器换成每日构建版（最后一张牌）；换不成也照旧再试一次
            if not ytdlp.escalate(params, report, job):
                report("download", None, "3 秒后最后一次重试…")
                time.sleep(3.0)

    # 优先用 yt-dlp 自己报的真实路径（--print-to-file 写的，见 _ytdlp_args）
    told = _told_file(out_dir)
    if told is not None:
        return told
    # 兜底：老办法，取目录里最新的 mp4
    mp4s = sorted(out_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    if not mp4s:
        raise RuntimeError("下载完成但没找到 mp4 文件")
    return mp4s[-1]


def _told_file(out_dir: Path) -> Path | None:
    """读 yt-dlp 用 --print-to-file 落下的最终路径；没有/读不到就返回 None（调用方走兜底）。

    倒着找最后一个**真实存在**的文件：跑合集的话它会往同一个文件里追加多行。
    """
    f = out_dir / ".last-file"
    try:
        if not f.exists():
            return None
        for line in reversed(f.read_text(encoding="utf-8", errors="replace").splitlines()):
            s = line.strip()
            if s and Path(s).exists():
                return Path(s)
    except Exception:  # noqa: BLE001 - 读不到就退回老办法，不影响下载
        pass
    return None


def _read_token_usage(srt_path: Path) -> str:
    """从 finesub 的任务报告里取本次翻译的 token 用量（拿不到就返回空串）"""
    try:
        reports = list(srt_path.parent.glob("*.llm-artifacts/task-report.md"))
        if not reports:
            return ""
        txt = reports[0].read_text(encoding="utf-8", errors="replace")
        line = next((l for l in txt.splitlines() if "task total" in l), "")
        if not line:
            return ""
        vals: dict[str, str] = {}
        for part in line.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().split()[-1].strip(":")   # 取 "key=值" 里的 key
                vals[k] = v.strip()
        inp = int(vals.get("total_input_tokens", "0") or 0)
        out = int(vals.get("output_tokens", "0") or 0)
        think = int(vals.get("thinking_tokens", "0") or 0)
        if inp or out:
            extra = f"，其中思考 {think:,}" if think else ""
            return f"本次翻译 LLM 用量：输入 {inp:,} tokens / 输出 {out:,} tokens{extra}"
    except Exception:
        pass
    return ""


def _current_home(params: dict) -> str:
    """这次实际会用到的引擎数据目录（空 = 引擎默认那个）"""
    return (params.get("finesub_home") or "").strip() or str(datadir.default_home())


def _deepseek_web_extra(video: Path, params: dict, report) -> str:
    """DeepSeek 联网查证：搜一次视频标题，把摘要拼成给翻译模型的背景资料。

    只做一次搜索（不是逐段检索），结果并进 `extra_info`；失败绝不影响翻译。
    """
    try:
        import deepseek_web
        title = str(params.get("_video_title") or "").strip()
        if not title or title in ("原始视频", "视频", "video"):
            title = str(params.get("extra_info") or "").strip()
        if not title:
            report("translate", None, "DeepSeek 联网查证：没有可搜索的标题，跳过")
            return ""
        r = deepseek_web.search(title, count=5, need_content=False)
        if not r.get("ok"):
            report("translate", None, f"DeepSeek 联网查证没搜到：{r.get('error') or '未知错误'}")
            return ""
        results = r.get("results") or []
        picked = [x for x in results if str(x.get("summary") or "").strip()][:3]
        if not picked:
            report("translate", None, "DeepSeek 联网查证：结果没有可用摘要，跳过")
            return ""
        lines = ["【实时网页资料：DeepSeek 搜索，供翻译核对专有名词和事实】"]
        for i, x in enumerate(picked, 1):
            t = str(x.get("title") or "").strip()
            site = str(x.get("site_name") or "").strip()
            summary = " ".join(str(x.get("summary") or "").split())
            lines.append(f"{i}. {t}" + (f"（{site}）" if site else "") + "：" + summary)
        block = "\n".join(lines)[:4000]
        report("translate", None,
               f"DeepSeek 联网查证：搜到 {len(results)} 条，已注入 {len(picked)} 条摘要")
        return block
    except Exception as e:  # noqa: BLE001 - 查证失败不能拖垮翻译
        report("translate", None, f"DeepSeek 联网查证失败（不影响翻译）：{type(e).__name__}: {e}")
        return ""


def _translate_zh_en(video: Path, params: dict, job: Job, report) -> Path:
    """中文 → 英语：先用引擎做中文 ASR，再用 DeepSeek 翻成英文。

    finesub 的翻译提示词写死输出简体中文，所以这一步**不能**走它的翻译阶段：
    `--stage raw-srt` 只做识别（语言 zh），英文由 en_translate 自己翻。
    """
    key = (params.get("deepseek_key") or "").strip()
    if not key:
        raise RuntimeError("中文 → 英语需要 DeepSeek Key，请到「设置 → API Key」里填写")
    # 引擎只跑识别：source_lang 换成 zh（引擎的工作目录也会按 zh 建）
    asr_params = dict(params)
    asr_params["source_lang"] = "zh"
    report("translate", 0, "中文 → 英语：先做中文语音识别…")
    raw = _translate_once(video, asr_params, job, report, stage="raw-srt")
    work = _work_dir(video, params)
    work.mkdir(parents=True, exist_ok=True)
    # 把中文 raw SRT 也放进 zh-en 的工作目录：配音要靠它判断「字幕换行处是不是句子边界」
    zh_raw = work / f"{video.stem}-raw.srt"
    if raw.resolve() != zh_raw.resolve():
        # 内容没变就别重写：保住 mtime，下面「英文翻译已有结果」的缓存才命中
        if not zh_raw.is_file() or zh_raw.read_bytes() != raw.read_bytes():
            zh_raw.write_bytes(raw.read_bytes())
    out = work / f"{video.stem}.srt"
    for stale in (f"{video.stem}-translated.srt", f"{video.stem}-corrected.srt"):
        (work / stale).unlink(missing_ok=True)
    if zh_raw.is_file() and out.is_file() and out.stat().st_mtime >= zh_raw.stat().st_mtime:
        report("translate", None, "英文翻译已有结果，直接复用")
    else:
        en_translate.translate_srt(raw, out, key, params.get("extra_info") or "", report,
                                   params=params)
    report("translate", _map("translate", 1.0), "英文翻译完成")
    return out


def _pause_for_review(srt: Path, job: Job, report, kind: str,
                      source: Path | None = None) -> Path:
    """停在暂停点，等用户点「继续」；返回用户选定的字幕（没改就是原稿）。"""
    job.review_kind = kind
    job.review_path = str(srt)
    job.review_source = str(source) if source is not None and source.is_file() else ""
    job.review_continue_path = ""
    job.review_event.clear()
    job.review_ready = True
    try:
        report("translate", None, "翻译完成，暂停等你检查译文…")
        while not job.review_event.wait(0.3):
            job.check()
        job.check()
        chosen = Path(job.review_continue_path or srt)
        return chosen if chosen.is_file() else srt
    finally:
        job.review_ready = False
        job.review_path = ""
        job.review_source = ""
        job.review_kind = ""


def _review_final_subtitles(srt: Path, params: dict, job: Job, report,
                            source: Path | None = None) -> Path:
    """翻译完成后暂停，等用户对照原文检查译文；返回最终拿去配音/压制的字幕。"""
    if not params.get("subtitle_review") or params.get("_in_batch"):
        return srt
    chosen = _pause_for_review(srt, job, report, "final", source)
    _refresh_style(params)          # 暂停期间改的字体/颜色/双语要能跟上，见函数注释
    report("translate", None, "继续配音 / 压制…")
    return chosen


#: 暂停点之后**重新读一次**的字幕外观设置。
#: 整份设置是在开工那一刻拍快照的（见 app.start），所以运行中改什么都不影响这一单 ——
#: 唯一例外就是这个暂停点：用户是看着字幕去调样式的，改完继续却不生效会很像 bug。
#: ⚠️ 只同步「外观」，不同步 do_dub / burn_subs / 语言 / 密钥那类会改变流程的键。
_LIVE_STYLE_KEYS = (
    "subtitle_font", "subtitle_font_file", "subtitle_size", "subtitle_spacing",
    "subtitle_color", "subtitle_outline", "subtitle_outline_color",
    "subtitle_margin_v", "subtitle_bilingual", "subtitle_bilingual_order",
    "subtitle_bilingual_scale",
)


def _refresh_style(params: dict) -> None:
    """把用户刚在设置里调过的字幕外观同步进本次任务（只影响还没跑的配音/烧录）。"""
    try:
        import settings as cfg
        live = cfg.load()
    except Exception:  # noqa: BLE001 - 读不到就照旧，别因为设置文件把任务搞挂
        return
    for k in _LIVE_STYLE_KEYS:
        if k in live:
            params[k] = live[k]


def translate(video: Path, params: dict, job: Job, report, attempts: int = 3,
              stage: str = "") -> Path:
    """finesub 翻译（带临时性失败自动重试），返回成品中文 SRT 路径

    stage="raw-srt" 时只跑到「原始字幕」（人声分离 + ASR + 字幕稳定化），
    **一个 LLM 都不调** —— 「只提取原文字幕」那个模式走这条路，不花 DeepSeek 额度。
    """
    if not stage and (params.get("source_lang") or "").strip().lower() == "de":
        # 引擎的翻译提示词是按日语场景写的（「日本轻小说感」那些）。德语走同一个
        # 模型没问题，但得把这条说明塞进 extra_info，别让译文带出日语语气。
        # ⚠️ 必须改**调用方那个 dict**，不能 `params = dict(params)`：`_work_dir()`
        #    会把 extra_info 算进任务目录哈希，只改副本的话翻译和配音会各自
        #    指向不同目录，配音那步就找不到 `-raw.srt`（实测过）。
        old = str(params.get("extra_info") or "").strip()
        if "源语言是德语" not in old:
            hint = ("【语言】源语言是德语。请译成自然、通顺的简体中文；"
                    "不要套用日语语气词、敬语或轻小说风格。")
            params["extra_info"] = (hint + ("\n\n" + old if old else "")).strip()
    if not stage and params.get("deepseek_web_search") and not _is_fast(params):
        params = dict(params)
        block = _deepseek_web_extra(video, params, report)
        if block:
            old = str(params.get("extra_info") or "").strip()
            params["extra_info"] = (old + "\n\n" + block).strip()
    last = ""
    n = 0
    healed = False
    net_fixed = False
    prepared = False
    while n < attempts:
        try:
            if not stage and (params.get("source_lang") or "") == "zh-en":
                return _translate_zh_en(video, params, job, report)
            if not stage:
                got = _chinese_subtitle(video, params, report)
                if got is not None:
                    return got
            if not stage and _is_fast(params):
                return _fast_translate(video, params, job, report)
            # 用户导入的字幕/文稿只准备一次：放好了就让引擎跳过识别；
            # 读不了就把这个参数摘掉，后面照常走 YouTube 字幕 / 语音识别。
            if not prepared:
                if not _prepare_user_subtitle(video, params, job, report):
                    params.pop("subtitle_file", None)
                prepared = True
            return _translate_once(video, params, job, report, stage=stage)
        except datadir.MountBlocked:
            # 数据目录被 Windows 拒绝建链接（os error 448）：换一个目录重来一次。
            # 预先探过还失败，说明探测和引擎的实际行为有出入，所以这里再兜一次底。
            if healed:
                raise
            healed = True
            before = _current_home(params)
            # 记下来：这台机器上探针会误判（实测过），下次直接从候选里排除
            datadir.mark_bad(before)
            datadir.ensure(params, report, exclude=[before], force=True)
            if _current_home(params) == before:
                raise          # 没有别的可用目录了，把中文提示原样抛给用户
            report("translate", None, f"换个数据目录重试（{_current_home(params)}）…")
        except GithubBlocked as e:
            # 引擎要装的组件只放在 GitHub 上（无代理的国内用户下不动）：现场换成
            # 国内加速镜像 + 本机自带副本，再重试一次。见 wheels.py / mirrors.py
            if net_fixed:
                raise
            net_fixed = True
            report("translate", None, str(e))
            mirrors.pick(params, report, force=True)
            wheels.prepare(params, report)
            # 引擎说它下不动 GitHub 的东西 —— 正是改清单地址的时候
            engine_mirror.apply(report)
            report("translate", None, "已改用镜像 / 本机分发，重新开始这一步…")
        except TransientError as e:
            last = str(e)
            n += 1
            if n >= attempts:
                break
            report("translate", None, f"⚠ 网络/服务抖动，10 秒后自动重试（第 {n + 1}/{attempts} 次）…")
            for _ in range(20):  # 可取消的等待
                job.check()
                time.sleep(0.5)
    raise RuntimeError(("finesub 字幕提取失败" if stage else "finesub 翻译失败")
                       + f"（已重试 {attempts} 次）: {last}")


def _write_finesub_env(params: dict) -> str:
    """把用户自己申请的翻译服务 key 写成 finesub 的 env 文件（不动全局配置）

    finesub 的 key 变量约定：FINESUB_KEY_<TIER>（如 FINESUB_KEY_DEEPSEEK）、
    Gemini 免费池为 GEMINI_FREE。返回文件路径；用户没填 key 时返回空串（用全局配置）。
    """
    lines = []
    if params.get("deepseek_key"):
        lines.append(f"FINESUB_KEY_DEEPSEEK={params['deepseek_key']}")
    # Gemini **只服务两个可选增强**（视频语境纠错、联网查证），翻译本身走 DeepSeek。
    # 两个增强都关着、又有 DeepSeek 时别把 Gemini key 交给引擎：引擎会把它排进候选
    # 链路，而国内直连 Google 是连不上的（客户实测「填了 Gemini key 之后，纠错翻译卡在
    # WinError 10060 超时」）。**判断口径必须和 llmroute 排的候选链一致**——链里有
    # Gemini 才给 key，否则那一档永远是"不可用"（v1.6 起由 llmroute 统一决定）。
    needs_gemini = bool(params.get("video_context") or params.get("web_search"))
    if params.get("gemini_key") and (needs_gemini or llmroute.wants_gemini(params)):
        lines.append(f"GEMINI_FREE={params['gemini_key']}")
    if not lines:
        return ""
    p = Path(os.environ.get("APPDATA", "")) / "StarryVT" / "finesub.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


# 引擎任务目录里的**中间**字幕：-raw 是原始识别、-corrected 是纠错后的源语言文字，
# 两份都是**源语言**（不是中文）。只有 <任务名>.srt 是最终的中文成品。
_SRT_INTERMEDIATE = ("-raw.srt", "-corrected.srt", "-stable.srt", "-aligned.srt", "-vad.srt")


def _final_srt(video: Path, home: Path, announced: Path | None) -> Path | None:
    """在引擎的任务目录里认「成品」字幕，认不出来返回 None

    **不能拿最后一次「完成：」报的路径直接当成品**：引擎跑到「[4/4] 原始字幕」
    就会报一次「完成：…-raw.srt」，那一份是源语言的。客户实测报过
    「点开始翻译只有英文字幕 / 日文字幕」——拿到手的就是这些中间文件。
    所以按名字认成品；认不出来才退回引擎报的路径（且必须不是中间文件），
    实在没有就**报错**，不要闷声把外文字幕当成品交出去。
    """
    dirs: list[Path] = []
    if announced is not None and announced.parent.is_dir():
        dirs.append(announced.parent)              # 本次这一跑的任务目录，优先
    try:
        runs = [d for d in (home / "tasks").glob(f"{video.stem}-*") if d.is_dir()]
    except OSError:
        runs = []
    dirs += sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True)

    for d in dirs:
        for name in (f"{video.stem}.srt", f"{video.stem}-translated.srt"):
            p = d / name
            if p.is_file():
                return p
    if (announced is not None and announced.is_file()
            and not announced.name.endswith(_SRT_INTERMEDIATE)):
        return announced                           # 引擎换了命名时的兜底
    return None


# ---------------- YouTube 字幕当源文字（省掉一次语音识别） ----------------

def _finesub_home(params: dict) -> Path:
    """引擎的数据根目录（和 _translate_once / _final_srt 用的是同一套规则）"""
    if params.get("finesub_home"):
        return Path(params["finesub_home"])
    return Path(os.environ.get("LOCALAPPDATA", "")) / "FineSub"


def _tasks_root(params: dict) -> Path:
    """任务目录的根：`finesub relocate` 可以把大文件搬到别的盘，得看 locations.json"""
    home = _finesub_home(params)
    try:
        import json
        loc = json.loads((home / "locations.json").read_text(encoding="utf-8"))
        big = (loc.get("bigData") or "").strip()
        if big and Path(big).is_dir():
            return Path(big) / "tasks"
    except Exception:
        pass
    return home / "tasks"


def _upgrade_old_annotated_csv(video: Path, params: dict, report=None) -> None:
    """把 0.5.x 留下的 `-annotated.csv` 补上 `spk` 列（0.6.0 起的位置契约）

    引擎按「同一个输入」复用旧任务目录。旧 CSV 只有 9 列，0.6.0 的知识更新
    会报 "annotated.csv has too few fields"，整单翻译跟着失败。这是派生缓存，
    原地补一列空 spk 就行，不用重新识别/翻译。只认 9 列的旧格式，别的版本不碰。
    """
    try:
        dirs = [d for d in _tasks_root(params).glob(f"{video.stem}-*") if d.is_dir()]
    except OSError:
        return
    for d in dirs:
        for csv in d.glob("*-annotated.csv"):
            try:
                text = csv.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = text.splitlines()
            if not lines or "spk" in lines[0]:
                continue
            head = lines[0].lstrip("# ").split("|")
            if len(head) != 9 or head[-1] != "note":
                continue
            out: list[str] = []
            for line in lines:
                if not line or line.startswith("#"):
                    out.append(line)
                    continue
                parts = line.split("|", 8)
                if len(parts) < 9:
                    out.append(line)          # 残缺行原样留着，别越改越坏
                    continue
                parts.insert(8, "")           # spk 在 char_count 与 note 之间
                out.append("|".join(parts))
            hp = lines[0].split("|", 8)
            hp.insert(8, "spk")
            out[0] = "|".join(hp)
            bak = csv.with_name(csv.name + ".bak-0.5")
            try:
                if not bak.exists():
                    bak.write_text(text, encoding="utf-8")
                csv.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""),
                               encoding="utf-8")
            except OSError:
                continue
            if report:
                report("translate", None, f"旧任务的对照表已升级格式：{csv.name}")


def _migrate_engine_user_config(params: dict, report=None) -> None:
    """0.6 删掉 planning-text / planning-mm / search_judge 这些任务格子。

    引擎会校验所有 presets，旧版用户配置（user-data/config.toml）里只要留着
    这些 bindings，就会报 ModelRouteConfigError，整单翻译失败。备份后删掉这几行。
    """
    home = _finesub_home(params)
    keys = ('"planning-text/quality"', '"planning-mm/quality"', '"search_judge/quality"')
    for cand in (home / "user-data" / "config.toml", home / "config.toml"):
        if not cand.is_file():
            continue
        try:
            text = cand.read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(k in text for k in keys):
            continue
        try:
            bak = cand.with_name(cand.name + ".bak-0.5")
            if not bak.exists():
                bak.write_text(text, encoding="utf-8")
            lines = [ln for ln in text.splitlines() if not any(k in ln for k in keys)]
            cand.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""),
                            encoding="utf-8")
            if report:
                report("translate", None, "旧版引擎路由配置已升级（0.6 兼容）")
        except OSError:
            pass


def _work_dir(video: Path, params: dict) -> Path:
    """这个视频的产物目录（用 `-o` 指给引擎）

    名字 = `<视频名>-<输入路径前 8 位哈希>`：
      * 带哈希 → 两个都叫「01. Intro」的视频不会共用中间结果（那是会出错的复用）
      * 同一个文件重跑还是这个目录，引擎按产物存在跳过的缓存照旧生效

    ⚠️⚠️ **哈希必须包含「会影响产物的设置」**（2026-10-17 实测出来的 bug）：
        只哈希输入路径的话，用户把「源语言」从英语改成日语、重跑同一个文件 ——
        引擎看到 `-raw.srt` 已存在，**六个阶段全部「已有结果，跳过」**，
        整条跑完只要 2 秒，拿回的是**上一版产物**，而界面上显示的是「成功」。
        同样中招的：换模型、改视频语境 / 联网查证 / 额外提示词。
        （三处对不上：识别是按语言做的、翻译是按模型+提示词做的、最终字幕跟着前两步变。）
    引擎那边**没有**「配置变了就重跑」的机制（它只按产物存在与否跳过），所以只能我们这侧兜。
    """
    try:
        import hashlib
        # 影响产物内容的设置（白名单：只放会改变输出文本的，别把纯界面项塞进来）
        affect = "\x00".join(str(params.get(k) or "") for k in (
            "source_lang", "video_context", "web_search", "deepseek_web_search", "extra_info"))
        # 模型/路由/上限的变化也要算进来 —— 换模型等于换译者（llmroute.render 是它的全量声明）
        try:
            cat, cfg = llmroute.render(params)
            route = cat + "\x01" + cfg
        except Exception:  # noqa: BLE001 - 路由文件生不出来时不影响建目录
            route = ""
        # 引擎自己的版本也要算进来：0.5→0.6 时 `-annotated.csv` 的列数变了，
        # 复用旧任务目录会在知识更新阶段报 "annotated.csv has too few fields"。
        # 用 exe 的大小+mtime 当指纹（升级/重装都会变），不依赖引擎的 --version。
        try:
            eng_exe = engine.find_finesub(params.get("finesub_exe") or "")
            st = Path(eng_exe).stat() if eng_exe else None
            eng_sig = f"{st.st_size}:{int(st.st_mtime)}" if st else ""
        except Exception:  # noqa: BLE001
            eng_sig = ""
        raw = f"{video.resolve()}\x00{affect}\x00{route}\x00{eng_sig}"
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        key = "work"
    return _tasks_root(params) / f"{video.stem}-{key}"


def _plant_youtube_srt(work: Path, video: Path, srt_text: str, stable_text: str,
                       report) -> Path | None:
    """把 YouTube 字幕写成引擎的产物，让引擎跳过人声分离 + 语音识别 + 稳定化

    两个文件都要在**启动引擎之前**写好（阶段是按产物存在跳过的）：
      `<任务名>-raw.srt`     第四阶段的产物，翻译直接读它
      `<任务名>-stable.json` 第三阶段的产物——引擎靠它判断「语音识别不需要」
                             （`aligned_required = 没有 stable.json`），
                             识别不需要时连人声分离都不跑
    """
    base = work / f"{video.stem}-raw.srt"
    fresh = True
    try:
        work.mkdir(parents=True, exist_ok=True)
        if base.exists():
            old = base.read_text(encoding="utf-8", errors="replace")
            if old == srt_text:
                fresh = False                    # 内容没变，别重写（保住上次的时间戳）
            else:
                for stale in (f"{video.stem}-translated.srt", f"{video.stem}-corrected.srt",
                              f"{video.stem}.srt"):
                    (work / stale).unlink(missing_ok=True)
        if fresh:
            base.write_text(srt_text, encoding="utf-8")
        # stable.json 每次都重写：它是我们放的标记（清洗时要靠它认人），
        # 旧版本放的可能还没有标记，沿用旧文件会让「关掉字幕」这个开关失灵
        (work / f"{video.stem}-stable.json").write_text(stable_text, encoding="utf-8")
    except OSError as e:
        report("translate", None, f"（字幕没能交给引擎，照常用识别：{e}）")
        return None
    if fresh:
        report("translate", None,
               f"交给引擎的字幕稿: {base.name}（跳过人声分离与语音识别，直接用 YouTube 字幕）")
    else:
        report("translate", None,
               f"沿用上次的字幕稿: {base.name}（跳过人声分离与语音识别）")
    return base


def _clear_planted_srt(work: Path, video: Path, report) -> None:
    """撤掉上次塞进去的字幕稿

    用户在设置里把字幕关了、或者这次的视频没字幕可拉时都要撤：产物还在的话，
    引擎会原样复用上次那份字幕，用户看到的就是「改了设置没反应」。
    只认自己放的那份（stable.json 里有 youtube-captions 标记，引擎自己跑出来的没有），
    不是我们放的一个字节都不动。
    """
    stable = work / f"{video.stem}-stable.json"
    try:
        if not stable.is_file():
            return
        head = stable.read_text(encoding="utf-8", errors="replace")[:600]
    except OSError:
        return
    if "youtube-captions" not in head and "user-subtitles" not in head:
        return
    done = False
    for name in (f"{video.stem}-stable.json", f"{video.stem}-raw.srt",
                 f"{video.stem}-translated.srt", f"{video.stem}-corrected.srt",
                 f"{video.stem}.srt"):
        try:
            (work / name).unlink(missing_ok=True)
            done = True
        except OSError:
            pass
    if done:
        report("translate", None, "已清掉上次的字幕稿，重新识别")


# ---------------- 用户导入的字幕 / 文稿 ----------------

#: 用户自带字幕稿的 stable.json 标记（清洗时靠它认人；别和 youtube-captions 混）
_USER_SUBTITLE_MARK = "user-subtitles"


def _user_subtitle_file(params: dict) -> Path | None:
    raw = (params.get("subtitle_file") or "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_file() else None


def _read_user_cues(path: Path, work: Path, report):
    """把用户导入的字幕文件读成 [(start, end, text)]；读不了返回 None。

    支持 SRT / VTT / ASS / SSA。ASS 用随包的 ffmpeg 转成 SRT 再读。
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".srt":
            return dubber.parse_srt(path) or None
        if suffix == ".vtt":
            got = ytsub.parse_srt(path)
            if got and got[0]:
                return got[0]
            return ytsub.parse_vtt(path) or None
        if suffix in (".ass", ".ssa"):
            tmp = work / f"{path.stem}.user-subtitle.srt"
            work.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([tools.path("ffmpeg"), "-v", "error", "-y",
                                "-i", str(path), str(tmp)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0 and tmp.is_file():
                return dubber.parse_srt(tmp) or None
    except Exception as e:  # noqa: BLE001 - 导入失败就退回语音识别
        report("translate", None, f"（读不了导入的字幕稿：{e}）")
    return None


def _read_script_lines(path: Path) -> list[str]:
    """读纯文稿：一行一句，去掉空行和 Markdown 的标题/列表符号。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        ln = re.sub(r"^#{1,6}\s+", "", ln)
        ln = re.sub(r"^[-*+]\s+", "", ln)
        if ln:
            lines.append(ln)
    return lines


def _align_script_to_segments(segs, lines):
    """把文稿按语音识别的时间轴摊到每一段上（best-effort，不切词）

    没有词级时间戳，只能按「每段时长占比 ≈ 文稿字数占比」来摊。逐字稿/讲稿够用；
    和视频内容对不上的稿子会明显错位（界面里已经提示过这一点）。
    """
    if not segs or not lines:
        return []
    if len(lines) == len(segs):
        return [(s, e, ln) for (s, e, _), ln in zip(segs, lines)]

    # 把文稿拆成 token，并记下它在整篇里的累计字符位置（中文按字，拉丁按词）
    tokens: list[tuple[int, int, str]] = []
    pos = 0
    for ln in lines:
        for m in re.finditer(r"[A-Za-z0-9'’\-]+|[一-鿿]|[^\s]", ln):
            tokens.append((pos, len(m.group()), m.group()))
            pos += len(m.group())
        pos += 1                      # 行与行之间留一个空位
    if not tokens:
        return []
    total_chars = max(1, pos)
    total_dur = sum(max(0.01, e - s) for s, e, _ in segs)
    if total_dur <= 0:
        return []

    # 每段结束时的累计时长 → 累计字符边界
    bounds: list[float] = []
    acc = 0.0
    for s, e, _ in segs:
        acc += max(0.01, e - s)
        bounds.append(acc / total_dur * total_chars)

    buckets: list[list[str]] = [[] for _ in segs]
    for start, length, tok in tokens:
        mid = start + length / 2
        i = 0
        while i < len(bounds) - 1 and mid > bounds[i]:
            i += 1
        buckets[i].append(tok)

    # 标点落在段首会很难看（「。 第二句」）：把它归给上一段
    punct = set("，。！？；：、,.!?;:)）】》」』…")
    for i in range(1, len(buckets)):
        while buckets[i] and buckets[i][0] in punct:
            buckets[i - 1].append(buckets[i].pop(0))

    def _join(parts: list[str]) -> str:
        text = " ".join(parts)
        # 中文之间不需要空格；标点前、左括号后也不需要空格
        text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)
        text = re.sub(r"\s+(?=[，。！？；：、,.!?;:)）】》」』…])", "", text)
        text = re.sub(r"(?<=[（【《「『])\s+", "", text)
        return text.strip()

    aligned = [(s, e, _join(parts)) for (s, e, _), parts in zip(segs, buckets)]
    aligned = [x for x in aligned if x[2]]
    return aligned or [(s, e, ln) for (s, e, _), ln in zip(segs, lines[:len(segs)])]


def _plant_user_cues(work: Path, video: Path, cues, source: str, report) -> bool:
    """把用户导入的字幕写成引擎的 -raw.srt + -stable.json。"""
    try:
        work.mkdir(parents=True, exist_ok=True)
        srt_text = ytsub.to_srt(cues)
        stable_text = ytsub.stable_json(
            cues, source=source, mark=_USER_SUBTITLE_MARK,
            note="由星译使用用户导入的字幕/文稿，未做语音识别")
        base = work / f"{video.stem}-raw.srt"
        if base.exists() and base.read_text(encoding="utf-8", errors="replace") == srt_text:
            pass
        else:
            for stale in (f"{video.stem}-translated.srt", f"{video.stem}-corrected.srt",
                          f"{video.stem}.srt"):
                (work / stale).unlink(missing_ok=True)
            base.write_text(srt_text, encoding="utf-8")
        (work / f"{video.stem}-stable.json").write_text(stable_text, encoding="utf-8")
        return True
    except OSError as e:
        report("translate", None, f"（字幕稿没能交给引擎，照常识别：{e}）")
        return False


def _find_sidecar_subtitle(video: Path) -> Path | None:
    """视频旁边同名的 .srt / .vtt（自动识别，不需要设置开关；2026-10-28 用户定）。"""
    for ext in (".srt", ".vtt"):
        p = video.with_suffix(ext)
        if p.is_file():
            return p
    return None


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _looks_chinese(cues: list) -> bool:
    """字幕正文是不是中文（用来决定「已有中文字幕」时跳过翻译）。

    只数正文里的汉字占比：中文字幕里夹着软件名、英文缩写很正常，阈值取 30%。
    """
    text = " ".join(str(t) for _s, _e, t in cues)
    chars = len(re.sub(r"\s", "", text))
    cjk = len(_CJK_RE.findall(text))
    return chars >= 20 and cjk >= chars * 0.3


def _chinese_subtitle(video: Path, params: dict, report) -> Path | None:
    """视频已经有中文（或中文字幕）时：直接拿来配音，不调翻译。

    2026-10-28 用户问「已有中文字幕的视频能跳过翻译直接配音吗」：只要字幕是
    中文，识别/机翻/纠错全部没必要。用户导入的字幕优先，其次是视频旁同名文件。
    """
    path, _side = _subtitle_source(video, params)
    if path is None:
        return None
    work = _work_dir(video, params)
    cues = _read_user_cues(path, work, report)
    if not cues or not _looks_chinese(cues):
        return None
    work.mkdir(parents=True, exist_ok=True)
    body = ytsub.to_srt(cues)
    (work / f"{video.stem}-raw.srt").write_text(body, encoding="utf-8")
    final = work / f"{video.stem}.srt"
    final.write_text(body, encoding="utf-8")
    report("translate", _map("translate", 1.0),
           f"已有中文字幕（{path.name}，{len(cues)} 条）：跳过翻译，直接配音")
    return final


def _subtitle_source(video: Path, params: dict) -> tuple[Path | None, bool]:
    """用户导入的字幕优先；没有就自动用视频旁的同名 .srt / .vtt。

    返回 (路径, 是否是「视频旁边自动找到的」)。第二个值只用来决定要不要在日志
    里说一句「发现视频旁的字幕」。
    """
    path = _user_subtitle_file(params)
    if path is not None:
        return path, False
    side = _find_sidecar_subtitle(video)
    return side, side is not None


def _prepare_user_subtitle(video: Path, params: dict, job: Job, report) -> bool:
    """优先使用用户导入的字幕/文稿。

    返回 True = 已放好，翻译阶段会跳过语音识别；False = 没放（文件不存在/读不了），
    照常走识别。纯文稿（.txt）没有时间轴，会先让引擎只跑 ASR（--stage raw-srt），
    再把文稿按时间轴摊到识别出来的每一段上。
    """
    path, sidecar = _subtitle_source(video, params)
    if sidecar and path is not None:
        report("translate", None, f"发现视频旁的字幕：{path.name}，直接用它")
    if path is None:
        if (params.get("subtitle_file") or "").strip():
            report("translate", None,
                   f"（找不到导入的字幕稿：{params.get('subtitle_file')}，改回语音识别）")
        return False

    work = _work_dir(video, params)
    if path.suffix.lower() == ".txt":
        report("translate", None, f"先用语音识别给文稿对齐时间轴：{path.name}")
        raw = _translate_once(video, params, job, report, stage="raw-srt")
        segs = dubber.parse_srt(raw)
        lines = _read_script_lines(path)
        cues = _align_script_to_segments(segs, lines)
        if not cues:
            report("translate", None, "（文稿是空的，或者识别结果读不出来，改回语音识别）")
            return False
        label = f"用户文稿 · {path.name} · {len(lines)} 行 → {len(cues)} 段"
    else:
        cues = _read_user_cues(path, work, report)
        if not cues:
            report("translate", None, f"（读不了导入的字幕稿 {path.name}，改回语音识别）")
            return False
        label = f"用户字幕 · {path.name} · {len(cues)} 条"

    if not _plant_user_cues(work, video, cues, path.name, report):
        return False
    params["_user_subtitle_planted"] = True
    report("translate", None, f"交给引擎的字幕稿：{label}（跳过语音识别）")
    return True

# ---------------- 配音稿：长度约束 + 音色克隆 ----------------

def _dub_style_rule() -> str:
    """给引擎的「配音稿」约束（只在真要配音时才加）

    中文念出来比英文长是常态，直译必然超时长 → 只能事后拿 ffmpeg 硬加速，
    用户听到的「突然加速」就是这么来的。这里从源头要短：**中文朗读约 5.5 字/秒**
    （实测 MiMo 冰糖；语速偏快 6.4），所以按「原文时长」给篇幅预算，
    让它写适合朗读的口语短句，而不是书面直译。
    """
    return ("这是配音稿，不是字幕稿：译文长度要贴近原文的朗读时长"
            "（中文约每秒 5.5 字），宁可精简、合并短句，不要逐字直译；"
            "去掉口头禅、重复和书面语，用适合朗读的口语。")


def _clone_reference(video: Path, params: dict, out_dir: Path, report) -> Path | None:
    """从引擎分离出的人声轨里剪一段参考音，给 MiMo 做音色克隆

    只取人声分离过的那条轨（背景音乐已经滤掉，克隆才干净）；剪 12 秒足够
    （官方口径「数秒即可」）。位置取全片 1/4 处，避开开头的寒暄和片尾。
    """
    if not params.get("tts_clone"):
        return None
    vocal = _work_dir(video, params) / f"{video.stem}-vocal.ogg"
    if not vocal.is_file() and (params.get("source_lang") or "") == "zh-en":
        # 中文→英语的识别是在 source_lang=zh 的工作目录里跑的，人声轨也在那边
        asr_params = dict(params)
        asr_params["source_lang"] = "zh"
        vocal = _work_dir(video, asr_params) / f"{video.stem}-vocal.ogg"
    if not vocal.is_file():
        report("dub", None, "（没找到人声轨，音色克隆用不上，改用所选音色）")
        return None
    dest = out_dir / "tts_cache" / "克隆参考音.wav"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    total = probe_duration(vocal)
    start = max(0.0, total * 0.25) if total else 0.0
    from job import no_window_kwargs
    r = subprocess.run([tools.path("ffmpeg"), "-v", "error", "-y",
                        "-ss", f"{start:.2f}", "-t", "12", "-i", str(vocal),
                        "-ac", "1", "-ar", "32000", str(dest)],
                       capture_output=True, text=True, **no_window_kwargs())
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size < 10000:
        report("dub", None, "（参考音没剪出来，改用所选音色）")
        return None
    report("dub", None, f"音色克隆：已从人声轨取 {dest.name}（第 {start:.0f} 秒起 12 秒）")
    return dest


def _stash_youtube_subs(tmp_dl: Path, out_dir: Path, stem: str, report) -> int:
    """把下载器拉下来的 .vtt 搬到视频旁边，文件名对齐视频名

    翻译那一步靠它跳过语音识别（`ytsub.find_vtt` 是按 `<视频名>.*.vtt` 找的），
    所以名字必须对上——视频这会儿已经改叫「原始视频.mp4」了。
    单独成函数是为了能脱离整条流水线单独测（见 .tools/ytsub-test）。
    """
    try:
        subs = sorted(tmp_dl.glob("*.vtt"))
    except OSError:
        return 0
    n = 0
    for sub in subs:
        parts = sub.name.split(".")
        lang = parts[-2] if len(parts) > 2 else "und"
        try:
            sub.replace(out_dir / f"{stem}.{lang}.vtt")
            report("download", None, f"已拿到 YouTube 字幕: {sub.name}")
            n += 1
        except OSError:
            pass
    return n


# ---------------- 引擎模型的下载进度（它自己不说，我们替它说）----------------

#: 引擎要下的 HuggingFace 权重：仓库目录名 → (人话名字, 字节数)
#: 名字和字节数抄自引擎自带的 `finesub_bootstrap/model-manifest.json`。
#: 引擎以后加了模型这里不认识也没关系——照样会报「已下载多少 MB」，
#: 只是算不出百分比。
_HF_REPOS = {
    "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo":
        ("语音识别模型（faster-whisper large-v3-turbo）", 1_617_884_929),
    "models--Qwen--Qwen3-ASR-0.6B-hf":
        ("语音校验模型（Qwen3-ASR 0.6B）", 1_564_928_088),
    "models--TransWithAI--whisper-ja-1.5B-ct2":
        ("日语识别模型（whisper-ja 1.5B）", 3_087_284_237),
}

#: 多久没有新字节就提醒一次（镜像断流时用户能立刻知道，不用干等）
_HF_STALL_SEC = 180


def _hf_hub_dir(params: dict) -> Path:
    """引擎放 HuggingFace 权重的地方：`<引擎数据目录>\\models\\huggingface\\hub`"""
    home = (params.get("finesub_home") or "").strip()
    root = Path(home) if home else Path(datadir.default_home())
    return root / "models" / "huggingface" / "hub"


def _hf_whisper_ready(params: dict) -> bool:
    """语音识别要用的权重是不是已经在本地了（在的话就不必开 verbose）

    ★ 不能只看 model.bin：加载器还要同目录的 config.json / tokenizer.json /
      vocabulary.json / preprocessor_config.json。旧版预取只下 model.bin，
      这四项缺一个，faster-whisper 就会报
      `Cannot load the vocabulary from the model directory`（2026-10-19 客户报障）。
    """
    repo = _hf_hub_dir(params) / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    need = ("config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json")
    try:
        for snap in (repo / "snapshots").glob("*/model.bin"):
            if all((snap.parent / n).is_file() for n in need):
                return True
        # 快照名可能和我们猜的不一样，退一步看 blob 里有没有那个 1.6GB 的大件；
        # 但这时小文件是否齐没法确认，交给开跑前的 modelfetch.repair_pipeline 补。
        return any(p.stat().st_size > 1_000_000_000
                   for p in (repo / "blobs").glob("*") if p.is_file())
    except OSError:
        return False


class _ModelWatch:
    """把「引擎正在下模型」这件事说出来。

    ★ 为什么必须我们盯：引擎取权重那一步是**全程静默**的（读 vendored 源码确认）——
      · `hf_weights.prepare` 那句「正在获取模型 …」走的是 `reporter.debug()`，
        normal 级别不打印；
      · 真正的下载跑在 `subprocess.run(..., capture_output=True)` 的子进程里，
        进度条全被吞掉；
      · 下载失败也被 `hf_weights._debug()` 吞掉，只剩一条 debug。
    合起来就是：**下成功看不见，下失败也看不见。**

    客户 2026-10-08 实测：界面停在 `[2/4] 语音识别` 26~44 分钟一动不动，
    只能当成死机（第一次翻译时 whisper 权重 1.62GB 还没下过）。

    引擎我们改不了（GPL，只调用不打包），所以自己盯 HF 缓存里那些 `.incomplete`
    文件的大小，至少让用户看得见它在动、断了也能立刻知道。
    """

    def __init__(self, params: dict, report):
        self._hub = _hf_hub_dir(params)
        self._report = report
        self._stop = threading.Event()
        self._speed = 0.0
        self._sizes: dict[str, int] = {}      # 上次轮询时每个 .incomplete 的大小
        self._growing: set[str] = set()       # 这次运行里真的长大过的那些
        self._last_repo = ""
        self._done_said = False

    def start(self):
        threading.Thread(target=self._loop, name="hf-watch", daemon=True).start()

    def stop(self):
        self._stop.set()

    # ---- 内部 ----
    def _active(self) -> tuple[str, int, int]:
        """（正在下的仓库名, 已下字节, 期望总字节）——没有在下的返回 ("", 0, 0)

        **只算「这次真的在长」的 `.incomplete`**（HF 库正在写、还没改名的临时文件）。

        为什么不能把 `.incomplete` 一律相加：客户 2026-10-08 实测报出
        `2060 MB / 1543 MB（133%）` —— 那是**上一次没下完留下的残片**也被算进去了。
        （残片的来路：`snapshot_download` 断点续传靠文件名里的 etag 后缀匹配，
        换过镜像/官方源之后对不上，就会另起一个 `.incomplete`，旧的留在盘上。）

        残片是死的、不会长大，所以「这次长过的」就是真在下的那一个。
        仓库下只有一个候选时不做这个判断 —— 没有歧义，而且第一次轮询还没观察到增长，
        界面会白等两秒。
        """
        got = want = 0
        active = ""
        now: dict[str, int] = {}
        try:
            repos = list(self._hub.glob("models--*"))
        except OSError:
            return "", 0, 0
        for repo in repos:
            try:
                parts = [p for p in (repo / "blobs").glob("*.incomplete") if p.is_file()]
            except OSError:
                continue
            if not parts:
                continue
            n = 0
            for p in parts:
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                key = str(p)
                now[key] = size
                prev = self._sizes.get(key)
                if prev is not None and prev != size:
                    self._growing.add(key)          # 第一次轮询只登记，不算"长过"
                if len(parts) == 1 or key in self._growing:
                    n += size
            if n <= 0:
                continue
            got += n
            want += _HF_REPOS.get(repo.name, ("", 0))[1] or n
            active = active or repo.name
        self._sizes = now
        return active, got, want

    def _label(self, repo: str) -> str:
        return _HF_REPOS.get(repo or self._last_repo, ("模型", 0))[0]

    def _loop(self):
        last_got, last_t = -1, time.monotonic()
        silent_since, warned = time.monotonic(), False
        while not self._stop.wait(2.0):
            repo, got, want = self._active()
            now = time.monotonic()
            if got <= 0:
                if last_got > 0 and not self._done_said:
                    # 刚才还在下、这会儿 `.incomplete` 没了 = 下完了（HF 会把临时文件改名成正式 blob）。
                    # 不说这一句的话，用户看着一个数涨到一半突然停住，会以为又出事了。
                    self._done_said = True
                    self._report("translate", None,
                                 f"⬇ {self._label(repo)}下载完成，引擎正在校验并加载…")
                continue                           # 没有在下的，一声不吭
            self._last_repo = repo
            if got == last_got:
                if not warned and now - silent_since > _HF_STALL_SEC:
                    warned = True
                    mins = max(_HF_STALL_SEC // 60, 1)
                    self._report("translate", None,
                                 f"⚠️ {self._label(repo)}下载已经 "
                                 f"{mins} 分钟没有进展——多半是模型镜像断了。"
                                 f"可以在「设置 → 高级选项」换一个 HF 镜像，"
                                 f"或者配上网络代理后重新开始。")
                continue
            dt = max(now - last_t, 0.001)
            if 0 <= last_got < got:
                self._speed = (got - last_got) / dt
            last_got, last_t = got, now
            silent_since, warned = now, False
            # 超过预期大小就不报百分比（残片让分子看起来超标时，宁可不给数也别给错的）
            pct = f"（{got * 100 // want}%）" if 0 < want and got <= want else ""
            spd = f" · {self._speed / 1048576:.1f} MB/s" if self._speed > 0 else ""
            # 前缀 ⬇ 是给界面日志看的：这类行永远不要被"同类信息折叠"，
            # 用户要看着它涨（见 app.Api._report）
            self._report("translate", None,
                         f"⬇ {self._label(repo)}下载中 {got / 1048576:.0f} MB / "
                         f"{want / 1048576:.0f} MB{pct}{spd}")


def _asr_language(params: dict) -> str:
    """引擎 ASR 用的语言。`zh-en` 是「中文识别 + 我们自己翻英文」，识别语言是 zh。"""
    lang = str(params.get("source_lang") or "en")
    return "zh" if lang == "zh-en" else lang


def _translate_once(video: Path, params: dict, job: Job, report, stage: str = "",
                    no_separate: bool = False) -> Path:
    env = _subprocess_env(params)
    if params.get("finesub_home"):        # 留空则用引擎自己的默认数据目录
        env["FINESUB_HOME"] = params["finesub_home"]
    env["FINESUB_HF_ENDPOINT"] = params.get("hf_endpoint") or "https://hf-mirror.com"
    # 没代理的用户：引擎自己要下的 ffmpeg / 人声分离模型等在 GitHub 上，
    # 这里换成国内加速镜像；有代理、或用户在设置里关了镜像时返回空。见 mirrors.py
    env.update(mirrors.env(params, report))
    env["PYTHONIOENCODING"] = "utf-8"
    env_file = _write_finesub_env(params)
    if env_file:
        env["FINESUB_ENV_FILE"] = env_file
    # 「翻译用哪个大模型」由我们指定：引擎打包的路由表只有 Gemini，国内直连必失败。
    # 这里把 DeepSeek 那条链写进引擎的模型目录/预设并指给它。见 llmroute.py
    env.update(llmroute.env(params))
    # 路由明细**不进界面日志**（2026-10-21 用户要求）：那行又长、带「失败」二字会被
    # Runner.Classify 判成红色的错误行，后面那句「退 Gemini 免费档」也容易让人以为
    # 出问题了。要看走了哪条链，去诊断报告（diag.py 里的「实际模型」）。
    # 引擎的 _read_dotenv 会读它自己的 .env（里面可能留着旧 key/keyring），但
    # _get_key_entries 先看进程环境变量。把当前设置里的 key 直接交给子进程，
    # 免得用户在设置里换了 key、引擎还在用旧的那把（实测 401）。
    if params.get("deepseek_key"):
        env["FINESUB_KEY_DEEPSEEK"] = str(params["deepseek_key"]).strip()
    if params.get("gemini_key") and (
            params.get("video_context") or params.get("web_search")
            or llmroute.wants_gemini(params)):
        env["GEMINI_FREE"] = str(params["gemini_key"]).strip()
    _migrate_engine_user_config(params, report)

    # 引擎路径：设置里填的 > PATH > 默认安装位置（find_finesub 会逐个校验存在性）
    finesub = engine.find_finesub(params.get("finesub_exe") or "")
    if not finesub:
        raise RuntimeError("找不到翻译引擎 finesub——请点界面上的「一键安装引擎」重新安装；"
                           "如果装过，检查杀毒软件是否把它隔离了。")
    # 模型缓存先补一遍：旧版预取只下了清单里的 model.bin，留下过一个
    # “看着完整、其实缺 vocabulary.json / config.json”的快照，引擎会给
    # faster-whisper 开 local_files_only，加载时报
    # `Cannot load the vocabulary from the model directory`（2026-10-19 客户报障）。
    # 齐了就是几次 stat，不联网；残缺时只补那几个小文件，不用清缓存重下几个 GB。
    try:
        import modelfetch
        modelfetch.repair_pipeline(params, job, report)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - 补不齐也让引擎自己去试
        report("translate", None, f"（模型缓存检查跳过：{e}）")
    # 0.5.x 的旧任务缓存里 annotated.csv 少一列（0.6.0 加了 spk），
    # 引擎复用旧目录时知识更新会崩；开跑前把它补成新格式。
    _upgrade_old_annotated_csv(video, params, report)
    # `--qwen-verify auto`：让引擎用它自带的 Qwen3-ASR 0.6B 复查可疑片段与漏词区间。
    # 这是引擎**默认就开**的一道质检（`auto` = 装了 transformers 5.x 就启用，
    # 缺依赖时它只警告、不报错），而我们从 v0.1.0 起一直传 `off` 把它关掉了——
    # 2026-10-13 实测：开着也就**多 5 秒**（模型加载，一段视频一次；可疑片段为 0 时
    # 它连一次解码都不做），换来的是「第二模型复核」这层保险，专治 ASR 把
    # 专有名词听错（`Unreal` 听成 `Unrull`、`C++` 丢掉这一类）。证据记在任务目录的
    # `-aligned.json` 里（`metadata.asr_align.qwen_verify`），诊断报告能一起收走。
    cmd = [finesub, str(video),
           "--language", _asr_language(params),
           # ⚠️ `--qwen-verify` 保持引擎默认（auto），**不要**拿它做「翻译质量」开关 ——
           #    2026-10-17 专门量过：同一段素材开/关各跑一遍（md5 相同、任务哈希不同），
           #    「纠错翻译」两边都是 2.1 分、合计都是 2.3 分，**一点没快**。
           #    它验的是**识别结果**（帮助原文 "verification evidence at the vad-asr tail"），
           #    不是译文；差别只有语音识别那一步的 3 秒。
           #    当时还以为它是大头，差点把一个「关掉只会更差、还省不到时间」的开关发给用户。
           "--qwen-verify", "auto"]
    # 产物目录由我们指定，路径就变得可预测——YouTube 字幕得在开跑**之前**放好
    # （引擎的阶段是按产物存在跳过的，第四阶段的产物就是 `<任务名>-raw.srt`）。
    # 命名规则：任务名 + 输入路径的短哈希，既躲开「同名视频互相串味」，又让
    # 同一个文件重跑时能接着上次的中间结果。
    work = _work_dir(video, params)
    cmd += ["-o", str(work / f"{video.stem}.srt")]

    if stage:
        # 只跑到指定阶段。目前只给「纯文稿对齐时间轴」用（--stage raw-srt）：
        # 这一步只要识别结果，不要 LLM 纠错翻译，所以下面那些 LLM 参数一个都不加。
        cmd += ["--stage", stage]
        if no_separate:
            cmd += ["--no-separate"]       # 快速模式：跳过人声分离
    else:
        cmd += ["--llm-correct-translate"]
        # 词条学习：引擎默认 `collect`（读词条库、**不写回**）。`update` 会在纠错翻译
        # 跑完之后再做一轮「知识更新」，把这次学到的术语写回用户的词条库 —— 这是
        # 「越用越准」的来源（打底那批见 builtin_kb.py）。默认开，设置里可关。
        # 0.6 起三档是 none / read / update（旧的 collect 已改名 read）
        cmd += ["--knowledge", "update" if params.get("learn_terms", False) else "read"]
        # 默认走纯文本路由：不把音视频上传到 Gemini（免费额度/地区限制下极易 400 失败），
        # 只用文本 LLM 纠错翻译；需要视频语境纠错时可把 settings 的 video_context 设为 True
        cmd += ["--llm-media", "video" if params.get("video_context") else "text"]
        # 联网查证走 Google 搜索，国内需代理且常被地区限制反复重试（实测能拖慢数倍），默认关闭
        cmd += ["--llm-retrieval", "native" if params.get("web_search") else "none"]
        if params.get("extra_info"):
            cmd += ["--extra-info", params["extra_info"]]
        # 长视频才开窗口并发（短视频开了反而慢，阈值见 _parallel_windows_for）。
        # probe 失败（0 秒）时不开，退回到原来的串行路径。
        cmd += _llm_parallel_args(dubber.probe_dur(video))
        # 要配音时再加一道「配音稿」约束：中文比英语长，不约束长度就只能事后硬加速
        if params.get("do_dub", True):
            cmd += ["--extra-style", _dub_style_rule()]

    # 引擎第一次用 whisper 权重时要在后台下 1.62GB，而那一步**什么都不打印**
    # （消息走 reporter.debug()，下载又跑在 capture_output 的子进程里）。
    # 把级别提到 verbose，至少能在日志里留下「正在获取模型 …」和它吞掉的报错。
    # 只在权重还没下齐时开：下齐之后 verbose 只会刷屏，没有信息量。
    if not _hf_whisper_ready(params):
        env["FINESUB_LOG_LEVEL"] = "verbose"

    watch = _ModelWatch(params, report)
    watch.start()

    # YouTube 字幕：转成整句 SRT，赶在引擎启动前放进产物目录
    # ⚠️ 以下三种情况不要动它：
    #   ① stage 非空 = 这次只要 ASR（给纯文稿对时间轴）→ 先清掉上次**我们放的**字幕稿，
    #      否则引擎看到 -raw.srt 已存在会直接跳过识别，拿旧字幕去对时间轴；
    #   ② 用户导入的字幕已经放好了 → 不能再用 YouTube 字幕盖掉它；
    #   ③ 其余照旧。
    if stage:
        _clear_planted_srt(work, video, report)
    elif params.get("_user_subtitle_planted"):
        pass
    elif (params.get("yt_subs") or "manual").lower() != "off":
        try:
            got = ytsub.build(video, (params.get("source_lang") or "en").split("-")[0])
        except Exception as e:  # noqa: BLE001
            got = None
            report("translate", None, f"（YouTube 字幕读取失败，照常识别：{e}）")
        if got:
            yt_srt, yt_stable, desc = got
            report("translate", None, f"发现{desc}，将用它替代语音识别")
            _plant_youtube_srt(work, video, yt_srt, yt_stable, report)
        else:
            _clear_planted_srt(work, video, report)
    else:
        _clear_planted_srt(work, video, report)

    try:
        p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", env=env,
                      cwd=str(video.parent))
        srt_path = None
        tail: list[str] = []
        for line in p.stdout:
            line = line.rstrip()
            tail.append(line)
            if len(tail) > 120:      # 留够行数，uv 报的错在长 traceback 的最上面
                tail.pop(0)
            _clock_note(line)        # 分阶段计时：认出 [n/4] 就结算上一段
            m = _RE_STAGE.match(line)
            if m:
                stage_i, pct = int(m.group(1)), m.group(2)
                sub = (stage_i - 1) / 4.0 + (int(pct) / 100.0 if pct else 0.0) / 4.0
                report("translate", _map("translate", sub), line)
                continue
            if "done" in line or "完成" in line:
                m2 = _RE_SRT_PATH.search(line)
                if m2:
                    srt_path = Path(m2.group(1))
            if line:
                report("translate", None, line)
        code = p.wait()
    finally:
        watch.stop()
        _ck().engine_done()

    # 失败时把「退出码 + 最后 120 行输出」攒成现场信息，交给 app 写进崩溃日志。
    # 没有它的话，报给用户的只有一句「finesub 翻译失败」——客户 12:01 那次
    # 「卡了 26 分钟然后失败」就是这么丢的：进程没吐任何堆栈，我们手里什么都没有。
    detail = (f"引擎退出码: {code}\n"
              f"命令: {' '.join(cmd)}\n"
              f"引擎数据目录: {params.get('finesub_home') or '(引擎默认)'}\n"
              f"----- 引擎最后 {len(tail)} 行输出 -----\n" + "\n".join(tail))

    def _fail(exc_cls, msg: str):
        e = exc_cls(msg)
        e.detail = detail
        return e

    if code != 0:
        # 用户点了「取消」→ 这条引擎子进程是我们自己 kill 掉的，退出码当然非零。
        # 不先判这一条的话，取消会被一路当成「翻译失败」，界面上报的是 ❌ 出错
        # 而不是「已取消」（2026-10-14 实测：第 25 秒取消，界面弹的是引擎失败）。
        job.check()
        blob = "\n".join(tail).lower()

        # 文件本身读不出视频流（坏了 / 不是视频）：这不是网络问题，重试多少轮都一样，
        # 得说清楚是**这个文件**的问题（不然用户会一直去查网络）。
        if any(k in blob for k in _BROKEN_MEDIA):
            raise _fail(RuntimeError,
                        "这个本地文件读不出视频流：已经损坏，或者它根本不是视频文件。"
                        "换一个文件试试（可以先用播放器打开确认一下）。")

        # 先判 448：这不是网络抖动，重试多少次都一样，得让用户看到该改哪里
        if datadir.looks_blocked(blob):
            raise _fail(datadir.MountBlocked,
                "引擎装不了 Python 运行环境：Windows 拒绝在数据目录里创建链接"
                "（os error 448「不受信任的装入点」，多半是网盘或搬家工具留下的重解析点）。\n"
                "已经自动试过其它磁盘；如果仍然不行，请到「设置 → 高级选项 → "
                "FINESUB_HOME（引擎数据目录）」手动选一个别的盘上的普通文件夹，然后重新开始。"
            )
        if _looks_github_blocked(blob):
            raise _fail(GithubBlocked,
                "引擎要从 GitHub 下载一个组件，但连不上（国内无代理时很常见）。"
                "正在改用国内加速镜像 / 本机自带的副本重试…"
            )
        # 再判「引擎连不上翻译用的 LLM」：10060 是"连不上"，不是"网络抖动"，
        # 归到下面去会白白重试三轮（客户实测每轮十几分钟）。
        hint = _llm_unreachable_hint(blob, params)
        if hint:
            raise _fail(RuntimeError, hint)
        hint = _llm_missing_hint(blob, params)
        if hint:
            raise _fail(RuntimeError, hint)
        # 引擎数据目录被占用/被拦：都不是网络问题，重试三轮纯属浪费用户时间
        hint = _busy_or_denied_hint(blob)
        if hint:
            raise _fail(RuntimeError, hint)
        # 启动器背后没有 Python（退出码 103）≠ 硬死，别给「内存/杀毒/显卡」那套
        hint = _engine_broken_hint(code, blob)
        if hint:
            raise _fail(RuntimeError, hint)
        # 一声不吭就没了（没有堆栈、退出码是异常码）：也不是网络抖动，别自动重试
        hint = _engine_crashed_hint(code, tail)
        if hint:
            raise _fail(RuntimeError, hint)
        if _looks_transient(blob):
            raise _fail(TransientError, "检测到网络/服务临时故障（SSL/连接/限流）")
        raise _fail(RuntimeError,
                    "finesub 翻译失败（看日志；常见原因：运行时未就绪 / LLM 额度 / 网络）")

    if stage:
        # 只跑了 ASR：产物是 `<任务名>-raw.srt`，交给 _prepare_user_subtitle 去对文稿
        raw = work / f"{video.stem}-raw.srt"
        if raw.is_file():
            report("translate", _map("translate", 1.0), "语音识别完成")
            return raw
        raise RuntimeError("语音识别完成但找不到原始字幕文件（-raw.srt）")

    report("translate", _map("translate", 1.0), "翻译完成")

    # 自定义了引擎数据目录时，tasks 也跟着走
    home = (Path(params["finesub_home"]) if params.get("finesub_home")
            else Path(os.environ.get("LOCALAPPDATA", "")) / "FineSub")
    final = _final_srt(video, home, srt_path)
    if final is None:
        raise RuntimeError("翻译完成但找不到成品 SRT 文件")
    if srt_path is None or final != srt_path:
        report("translate", None, f"取成品字幕: {final.name}")
    usage = _read_token_usage(final)
    if usage:
        report("translate", None, usage)
    return final


def _find_thumbnail(video: Path) -> Path | None:
    """找 yt-dlp 下载的同名封面图（--write-thumbnail 的产物）"""
    for ext in (".jpg", ".jpeg", ".webp", ".png"):
        p = video.with_suffix(ext)
        if p.exists():
            return p
    return None


def burn(video: Path, srt_path: Path, out_path: Path, job: Job, report,
         params: dict | None = None, src_srt: Path | str | None = None) -> Path:
    """烧字幕：SRT -> ASS -> ffmpeg（libsvtav1），返回终版路径

    src_srt 是**源语言字幕**（`-raw.srt`）。开了「双语字幕」时用它和译文逐行配对，
    一条字幕里放两行（原文 + 中文）；没开就完全用不到它。
    """
    work = out_path.parent
    ass = work / "subs.ass"
    p = params or {}
    # 字体：用户选了自己的字体文件就用它（族名从文件里读），没选就用随包的思源黑体。
    # ⚠️ libass 按**族名**找字形，ASS 里的 Fontname 必须和字体文件内部的族名一致，
    #    直接拿文件名写进去会掉回系统默认字体（见 subtitle_fonts.resolve）。
    font, font_file = subtitle_fonts.resolve(
        str(p.get("subtitle_font") or ""), str(p.get("subtitle_font_file") or ""))

    bilingual = bool(p.get("subtitle_bilingual"))
    src_for_ass = ""
    if bilingual:
        src = Path(src_srt) if src_srt else (_work_dir(video, p) / f"{video.stem}-raw.srt")
        if src.is_file():
            src_for_ass = str(src)
        else:
            report("burn", None, "⚠ 没找到原文对照字幕，这次只烧中文字幕")
    try:
        src_scale = float(p.get("subtitle_bilingual_scale") or 80)
    except (TypeError, ValueError):
        src_scale = 80.0
    n = srt2ass.srt_to_ass(
        srt_path, ass,
        font=font,
        size=float(p.get("subtitle_size") or 42),
        margin_v=int(p.get("subtitle_margin_v") or 30),
        outline=float(p.get("subtitle_outline") or 2.2),
        spacing=float(p.get("subtitle_spacing") or 0),
        color=str(p.get("subtitle_color") or "#FFFFFF"),
        outline_color=str(p.get("subtitle_outline_color") or "#000000"),
        bilingual=bool(src_for_ass),
        src_srt=src_for_ass,
        src_order=str(p.get("subtitle_bilingual_order") or "src_top"),
        src_scale=src_scale)
    if src_for_ass:
        where = "原文在上" if (p.get("subtitle_bilingual_order") or "src_top") != "src_bottom" else "原文在下"
        report("burn", _map("burn", 0.02),
               f"双语字幕（{where}，原文字号 {src_scale:g}%）：{n} 条")
    else:
        report("burn", _map("burn", 0.02), f"字幕样式生成（{n} 条）")

    # 可选：带上字体文件，libass 用 fontsdir 从工作目录里找（用户没装字体也能烧）
    vf = "ass=subs.ass"
    font_dir = None
    if font_file and Path(font_file).is_file():
        font_dir = work / ".fonts"
        font_dir.mkdir(parents=True, exist_ok=True)
        dest = font_dir / Path(font_file).name
        try:
            if not dest.is_file() or dest.stat().st_size != Path(font_file).stat().st_size:
                shutil.copyfile(font_file, dest)
            vf += ":fontsdir=.fonts"
        except OSError:
            font_dir = None

    dur = probe_duration(video) or 1.0
    # preset 10（2026-10-22 实测，123 秒素材）：比 preset 8 快 2.0×，体积 +2.4%，
    # PSNR 差 0.01 dB（25.185 → 25.171），肉眼看不出。preset 12 只再快 3% 但体积 +5.9%；
    # NVENC 只快 1.66× 且体积 +11%，都不如 10 划算。
    cmd = [tools.path("ffmpeg"), "-v", "error", "-y", "-i", str(video.resolve()),
           "-vf", vf, "-c:v", "libsvtav1", "-preset", "10", "-crf", "32",
           "-c:a", "copy", "-progress", "pipe:1", "-nostats", out_path.name]
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  text=True, encoding="utf-8", errors="replace", cwd=str(work))
    last = 0
    for line in p.stdout:
        m = _RE_FFPROG.search(line)
        if m:
            sec = int(m.group(1)) / 1e6
            frac = min(0.99, sec / dur)
            if frac - last > 0.01:
                last = frac
                report("burn", _map("burn", frac), f"压制中 {int(frac * 100)}%")
    err = p.stderr.read() if p.stderr else ""
    code = p.wait()
    if font_dir is not None:
        shutil.rmtree(font_dir, ignore_errors=True)
    if code != 0:
        raise RuntimeError(f"烧字幕失败: {err[-500:]}")
    return out_path


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}


def list_folder_videos(folder: Path, limit: int = 800) -> list[dict]:
    """列出文件夹里的视频，**连子文件夹一起找**（按路径排序）

    课程资料大多长这样：一个课程一个文件夹，里面一集一个子文件夹。只看第一层会一个都
    找不到（用户 2026-10-14 实测：Artisan 那套 Substance Designer 教程就是这么放的）。
    `rel` 是相对所选文件夹的位置，界面上用来告诉用户"这个在第几层"。

    顺手跳过我们自己会往里写东西的目录（output / _下载中），免得把上一次翻好的成片
    当成新素材再翻一遍 —— 只跳**顶层**那一个，用户自己深处建的 output 不受影响。
    """
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder}")
    skip_top = {"output", "_下载中", "tts_cache"}
    items: list[dict] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        parts = p.relative_to(folder).parts
        if parts and parts[0].lower() in skip_top:
            continue
        rel = str(p.parent.relative_to(folder))
        items.append({"title": p.name, "src": str(p), "rel": "" if rel == "." else rel})
        if len(items) >= limit:
            break
    return items


def list_playlist(url: str, params: dict, job: Job, report) -> list[dict]:
    """用 yt-dlp 读取合集/播放列表的所有条目（不下载）"""
    cmd = [tools.path("yt-dlp")]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    cmd += ["--flat-playlist", "--no-warnings", "--encoding", "utf-8",
            # flat 模式不给每条的 uploader（是 NA），但**合集自己**的作者和名字是有的 ——
            # 拿来当每集的作者，整条列表就都有作者可显示了
            "--print", "%(title)s\t%(url)s\t%(id)s\t%(duration_string)s"
                       "\t%(uploader)s\t%(playlist_uploader)s\t%(playlist_title)s", url]
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding="utf-8", errors="replace",
                  env=_subprocess_env(params))
    items, tail = [], []

    def na(x: str) -> str:
        return "" if x in ("NA", "none", "None") else x

    for line in p.stdout:
        line = line.rstrip()
        if "\t" in line:
            # 标题里偶尔真的有制表符，所以后面这几项一律从**右边**取
            parts = line.rsplit("\t", 6)
            while len(parts) < 7:
                parts.append("")
            t, u, vid, dur, who, by, coll = (na(x.strip()) for x in parts[:7])
            item = {"title": t or u, "src": u, "dur": dur,
                    "by": who or by,            # 单集作者，没有就用合集作者
                    "coll": coll, "coll_by": by}
            # 封面：flat 模式下 yt-dlp 不给 thumbnails 字段，但 YouTube 的缩略图
            # 是可以用视频 id 直接拼出来的（列表里显示封面，不用额外请求）
            if vid:
                item["thumb"] = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
            items.append(item)
        elif line:
            tail.append(line)
            if len(tail) > 20:
                tail.pop(0)
    if p.wait() != 0 or not items:
        raise RuntimeError(_friendly_download_error("\n".join(tail)) or "没能读到合集里的视频列表")
    report("download", 0, f"合集共 {len(items)} 个视频")
    return items


def run_batch(params: dict, job: Job, report, queue_cb=None) -> dict:
    """批量模式：处理文件夹里的全部视频，或 YouTube 合集的全部视频

    queue_cb(index, status, detail) 用于把每个条目的状态同步到界面队列。
    """
    base = resolve_output_base(params, report)
    src = (params.get("source") or "").strip()
    if not src:
        raise RuntimeError("请先选择文件夹或填写合集链接")
    batch_dir = base / f"批量-{time.strftime('%Y%m%d-%H%M%S')}"

    # 1) 解析条目
    if params.get("batch_kind") == "playlist":
        report("download", 0, "读取合集列表…")
        items = list_playlist(src, params, job, report)
    else:
        items = list_folder_videos(Path(src))
    if not items:
        raise RuntimeError("没有找到可处理的视频")
    total = len(items)
    if queue_cb:
        for i, it in enumerate(items):
            queue_cb(i, {"title": it["title"], "status": "pending", "detail": ""})
    report("download", 0, f"共 {total} 个视频，逐个处理（一个完成再接下一个）")

    results, failed = [], []
    for idx, item in enumerate(items):
        job.check()
        if queue_cb:
            queue_cb(idx, {"status": "running"})
        report("download", int(idx / max(1, total) * 100),
               f"—— [{idx + 1}/{total}] {item['title'][:60]} ——")
        try:
            r = run_pipeline({**params, "source": item["src"], "output_dir": str(batch_dir),
                              "_in_batch": True, "_batch_index": idx + 1},
                             job, report)
            results.append(r)
            if queue_cb:
                queue_cb(idx, {"status": "done", "detail": Path(r.get("final") or r.get("srt") or r.get("video") or "").name})
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 单条失败不中断整批
            failed.append((item["title"], str(e)))
            if queue_cb:
                queue_cb(idx, {"status": "failed", "detail": str(e)[:150]})
            report("error", None, f"⚠ 跳过失败项「{item['title'][:40]}」: {str(e)[:150]}")

    report("done", 100, f"🎉 批量完成：成功 {len(results)}，失败 {len(failed)}（输出目录：{base}）")
    return {"batch": True, "ok": len(results), "failed": len(failed),
            "out_dir": str(base), "results": results,
            "failed_items": [t for t, _ in failed]}


# ---------- 总入口 ----------

def _required_tools(params: dict) -> list[str]:
    """本次任务真正需要的外部程序（只出字幕就不需要 ffmpeg）"""
    need: list[str] = []
    if re.match(r"^https?://", (params.get("source") or "").strip(), re.I):
        need.append("yt-dlp")
    if params.get("do_dub", True) or params.get("burn_subs", True):
        need += ["ffmpeg", "ffprobe"]
    if params.get("download_only"):
        need.append("ffmpeg")            # yt-dlp 合流音视频要用
    return need


def _warn_google_enhancements(params: dict, report) -> None:
    """开了「视频语境纠错 / 联网查证」却没配代理时，开工前先说一声

    这两个增强都要连 Google（上传画面 / 联网搜索）。客户 2026-10-08 实测：他勾了
    「视频语境纠错」，日志里一路 `Gemini upload failed (Connect timeout)，attempt 1/3`
    —— 白等重试，而且那条增强实际上**完全没生效**（纠错退回到纯文本的 DeepSeek）。

    根因在界面上：那个勾选框当时只写「需 Gemini Key」，客户自然以为有 key 就够了。
    文案已经补上「国内要开代理」，这里再加一道 —— 界面上写着的东西用户不一定看，
    开工时说的话他一定看得见。

    只提示、不拦：万一他是靠系统级代理上网的，填不填设置里的代理框都拦不住。
    """
    if mirrors.effective_proxy(params):
        return
    on = [label for key, label in (("video_context", "视频语境纠错"),
                                   ("web_search", "联网查证"))
          if params.get(key)]
    if not on:
        return
    report("translate", None,
           "提示：你开了「" + "」「".join(on) + "」，这一步要连 Google 上传画面 / 联网搜索，"
           "但设置里没有填网络代理 —— 多半会一直连接超时、白等一会儿，"
           "而且这两个增强本身不会生效（纠错会自动退回纯文本）。\n"
           "只用 DeepSeek 翻译不需要代理，建议到「设置 → 高级选项」把它们取消勾选。")


def _precheck(params: dict, report) -> None:
    """开工前自检：外部程序齐不齐 + 引擎数据目录能不能用（见 datadir 说明）"""
    tools.refresh()
    miss = [n for n in _required_tools(params) if not tools.find(n)]
    if miss:
        raise tools.MissingTool(miss)
    if params.get("download_only"):
        # 只下载视频：不碰引擎、模型、Key，也没有「字幕识别的模型要下」那些提示
        return
    # ★ 引擎启动器背后的 Python 还在不在（2026-10-26 客户报障）。
    #   文件都在、解释器被删/变成坏链接的机器，以前会一路跑到引擎退出码 103，
    #   再被当成「内存不够 / 杀毒软件 / 显卡驱动」—— 客户永远修不好。
    fb = engine.find_finesub(params.get("finesub_exe") or "")
    if fb:
        problem = engine.shim_problem(fb)
        if problem:
            raise RuntimeError(
                problem + "\n请到「设置 → 翻译引擎」点「一键安装引擎」重新安装一次"
                "（只重装引擎本体，已经下好的运行环境和模型不会重下）。")
    # 上一轮留下的引擎进程还在跑？它握着 uv 缓存锁，再开一轮必然等 300 秒然后失败
    # （2026-10-08 客户实测）。
    # ★ 2026-10-21：没人管的残留直接收掉 —— 客户反馈「任务管理器里根本杀不掉」，
    #   与其让他去折腾，不如开工前我们自己清干净；另一个星译在用的才留着报错。
    # ★ 2026-10-26：启动时的内置词条导入会在后台连续 spawn `finesub.exe`，以前
    #   这里会把它当成残留杀掉、转头又看到新进程，报「后台还有没退干净的引擎进程」。
    #   先置 TASK_WAITING（词条导入看到就收手），再拿 CLI_LOCK 做清理；加锁期间
    #   不会有词条进程冒出来。
    engine.TASK_WAITING.set()
    try:
        if not engine.CLI_LOCK.acquire(timeout=1.0):
            report("translate", None,
                   "正在导入内置术语词条（只做一次），等它收尾后再开始…")
            engine.CLI_LOCK.acquire()
        try:
            busy = engine.busy_processes()
            r: dict = {}
            if busy:
                report("translate", None,
                       "发现上次没退干净的引擎进程：" + "、".join(busy) + "，正在结束…")
                r = engine.kill_leftovers(report, stage="translate")
                busy = engine.busy_processes()
            if busy:
                if r.get("skipped") and not r.get("remaining"):
                    raise RuntimeError(
                        "另一个星译窗口正在翻译（" + "、".join(busy) + "）。\n"
                        "等它跑完，或先关掉那个窗口，再重新开始。")
                raise RuntimeError(
                    "后台还有没退干净的引擎进程在跑：" + "、".join(busy) + "。\n"
                    "已经尝试结束但没成功，它多半卡在驱动或下载上。请重启电脑后再重新开始。")
        finally:
            engine.CLI_LOCK.release()
    finally:
        engine.TASK_WAITING.clear()
    # 第一次翻译要下 1.6GB 的语音识别模型，而引擎在下它的时候**一个字节都不打印**
    # （见 _ModelWatch 的说明）。先把话说在前面，免得用户以为死机了。
    if not _hf_whisper_ready(params):
        report("translate", None,
               "提示：第一次翻译要先下载语音识别模型（约 1.6 GB，视网速可能十几分钟）。"
               "这一步引擎自己不报进度，我们会持续报告「下载中…」，请耐心等待。")
    _warn_google_enhancements(params, report)
    home = datadir.ensure(params, report)
    # 运行环境还没装的话，这一趟要下 2.6 GB 的 torch。先过一遍微软运行库：
    # 缺了它 c10.dll 必然加载失败，引擎还会把 staging 删掉重来（2026-10-23 客户
    # 就是在这儿反复重下、一次没成）。装过运行环境的人跳过，不额外打扰。
    runtime_dir = Path(home) if home else datadir.default_home()
    if not datadir.runtime_ready(runtime_dir):
        import vcruntime
        if not vcruntime.ensure(params, report):
            raise RuntimeError(
                "翻译引擎的运行环境装不了：这台机器缺微软 VC++ 运行库，torch 加载不起来。\n"
                "按日志里的提示装好（或重启）之后，再重新开始。")
    # 引擎跑起来后要从 GitHub 拿几个组件（没代理下不动）：提前换成国内镜像 +
    # 本机自带的副本。这里失败不拦任务——真下不动时 translate() 还会再兜一次。
    try:
        mirrors.pick(params, report)
        wheels.prepare(params, report)
        # ★ 引擎清单里那 6 条 GitHub 地址：老版本曾经改到过 COS，这里恢复成原始地址。
        #   **必须挂在这儿，不能只挂在装引擎那一步**：已经装好引擎的人根本不会再走
        #   install_finesub，不在这儿恢复的话他们的清单会一直指向已经停用的 COS。
        engine_mirror.apply(report)
        # ffmpeg 资源缺失时改用钉死的日期构建，避免临时打 GitHub API（限流）。
        import runtime_pin
        runtime_pin.apply(params, report)
    except Exception as e:  # noqa: BLE001 - 预检尽力而为，不影响开工
        report("engine", None, f"（外网依赖预检跳过：{e}）")


# ============================================================ 分阶段计时
#
# 为什么要有（2026-10-17）：想给这条流水线提速时发现**整条流程没有任何耗时记录** ——
# 应用日志里没有，引擎的任务产物里也只有 VAD/ASR 的元数据。于是所有优化判断只能靠
# 「架构上应该是这样」去猜：那天先怀疑视频编码（以为换硬件编码器能省一大截），
# 实测 720p 只快 2.0×、1080p 1.6×，换算到整条流程才 1~3 分钟 —— 白测一轮。
# 有了这张表，下次动手前先看数。
#
# ⚠️ 它**只记录、不改变任何行为**（不调参数、不动顺序），跑失败也照样出表 ——
#    失败那次恰恰最需要知道卡在哪一步。
# ⚠️ 引擎那几步的名字**由引擎自己报**（它的输出是 `[n/4] 名字 45% · 3/7`），
#    **别去猜四个阶段叫什么** —— 猜错了这张表就开始骗人。

#: ⚠️⚠️ 引擎**同时打两套进度行**，两套都要认（2026-10-17 实测）：
#:     `[1/4] 人声分离`  ← 流水线级，`_RE_STAGE` 认的是这套（进度条靠它）
#:     `[1/6] 人声分离`  ← 详细级，六个阶段都有
#:   只认 `/4` 那套的话，**「纠错翻译」（最费时的一步）根本不会被认出来** ——
#:   它的耗时会全被算到前一个阶段头上，表面正常、数字全错。
#:   靠**名字去重**来合并两套（同名就当同一步，不重复计时）。
_RE_ANY_STAGE = re.compile(r"^\[\d+/\d+\]")
_RE_STAGE_LABEL = re.compile(r"^\[\d+/\d+\]\s*(.+)$")


def _stage_label(line: str) -> str:
    """从 `[n/m] 名字  后面还有别的` 里把名字抠出来。

    名字后面要么是**两个以上空格**（`人声分离     已有结果，跳过`），
    要么直接跟 `NN% ·`（`纠错翻译     100% · 1/1 windows`）。"""
    m = _RE_STAGE_LABEL.match(line)
    if not m:
        return ""
    return re.split(r"\s{2,}|\s+\d+%\s*·", m.group(1), maxsplit=1)[0].strip()


class _Clock:
    """一次运行的耗时表：我们自己的几个阶段 + 引擎内部那几步。"""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.phases: dict[str, float] = {}     # 下载 / 翻译（引擎）/ 配音 / 压制
        self.engine: dict[str, float] = {}     # 引擎自己报的那几步
        self._p: str | None = None
        self._p_at = self.t0
        self._e: str | None = None
        self._e_at = self.t0

    # ---- 我们自己的阶段 ----
    def enter(self, name: str) -> None:
        self._close_phase()
        self._p, self._p_at = name, time.time()

    def leave(self) -> None:
        self._close_phase()

    def _close_phase(self) -> None:
        if self._p:
            self.phases[self._p] = self.phases.get(self._p, 0.0) + (time.time() - self._p_at)
            self._p = None

    # ---- 引擎内部（从它的输出里认）----
    def engine_line(self, line: str) -> None:
        if not _RE_ANY_STAGE.match(line):
            return
        label = _stage_label(line) or "未命名阶段"
        if label == self._e:
            return                             # 两套行会重名（见 _RE_ANY_STAGE 的注释），同名不算换阶段
        self._close_engine()
        self._e, self._e_at = label, time.time()

    def _close_engine(self) -> None:
        if self._e:
            self.engine[self._e] = self.engine.get(self._e, 0.0) + (time.time() - self._e_at)
            self._e = None

    def engine_done(self) -> None:
        """引擎进程结束了：把最后一个阶段的计时收掉。

        不早收的话，最后一个「最终字幕」会一直计到整个流水线结束，
        把后面的配音 + 压制时间也算进引擎内部那一段（2 小时视频上实测多算 26 分钟）。
        """
        self._close_engine()

    def summary(self) -> str:
        self._close_phase()
        self._close_engine()

        def h(sec: float) -> str:
            return f"{sec:.0f}秒" if sec < 60 else f"{sec / 60:.1f}分"

        parts = [f"合计 {h(time.time() - self.t0)}"]
        own = " · ".join(f"{k} {h(v)}" for k, v in self.phases.items() if v >= 0.5)
        if own:
            parts.append(own)
        eng = " · ".join(f"{k} {h(v)}" for k, v in self.engine.items() if v >= 0.5)
        if eng:
            parts.append("引擎内：" + eng)
        return " ｜ ".join(parts)


#: 当前这次运行的表。一次进程只跑一个任务（批量也是顺序跑），所以一个模块级变量够用。
_CLOCK: "_Clock | None" = None


def _clock_note(line: str) -> None:
    """引擎每吐一行就喂进来（认不出阶段的行会被忽略）。"""
    if _CLOCK is not None:
        _CLOCK.engine_line(line)


def _ck() -> _Clock:
    """拿当前的表（理论上 run_pipeline 一定建好了；万一没有就现建一个，绝不让计时把流程搞崩）。"""
    global _CLOCK
    if _CLOCK is None:
        _CLOCK = _Clock()
    return _CLOCK


def run_pipeline(params: dict, job: Job, report) -> dict:
    """执行完整流水线（外面只包一层计时，见上面的 _Clock）。"""
    global _CLOCK
    clock = _CLOCK = _Clock()
    try:
        return _run_pipeline(params, job, report)
    finally:
        # 成败都出表 —— 失败那次最需要知道卡在哪一步
        report("timing", None, clock.summary())


#: 文件名翻译缓存：{(原名, 目标语言): 译名}，一次运行里同一个名字只翻一次
_NAME_TRANS: dict[tuple[str, str], str] = {}


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s or "")


def _clean_name(s: str) -> str:
    """把模型返回的文件名收拾干净：去引号/换行/Windows 非法字符，限长。"""
    s = re.sub(r"[\r\n\t]+", " ", (s or "").strip())
    s = s.strip().strip("\"'`“”‘’").strip()
    s = re.sub(r'[<>:"/\\|?*]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ._-")
    return s[:80].strip(" ._-")


def _translated_stem(video: Path, params: dict, report=None) -> str:
    """把视频文件名翻成目标语言（只用于**输出**的目录名/文件名，原文件不改）。

    设置 → 输出 → 翻译文件名和文件夹名。默认关；开了会对每个视频多一次很短的
    DeepSeek 请求。翻译失败、返回空、跟原名一样，全都退回原名，绝不让它挡住任务。
    """
    stem = (video.stem or "").strip()
    # 只提取原文字幕时**一次 DeepSeek 都不该调**（客户就是为了不花额度才用这个模式）
    if (not stem or not params.get("translate_names") or params.get("asr_only")
            or _is_fast(params)):
        return video.stem
    target_en = (params.get("source_lang") or "") == "zh-en"
    # 已经是目标语言就别浪费一次请求（中文名翻中文 / 英文名翻英文）
    if target_en and not _has_cjk(stem):
        return video.stem
    if not target_en and _has_cjk(stem):
        return video.stem
    key = (stem, "en" if target_en else "zh")
    hit = _NAME_TRANS.get(key)
    if hit:
        return hit
    api_key = str(params.get("deepseek_key") or "").strip()
    if not api_key:
        return video.stem
    if target_en:
        system = ("Translate the video file name into natural English. Keep numbers, "
                  "brand names and technical terms. Output only the translated name, "
                  "no quotes, no explanation.")
    else:
        system = ("把下面的视频文件名翻译成简体中文。保留数字、品牌名和技术术语。"
                  "只输出翻译结果，不要引号，不要解释。")
    try:
        out = en_translate._post({
            "model": en_translate.MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": stem}],
            "temperature": 0.1,
            "stream": False,
        }, api_key, timeout=30, params=params)
    except Exception as e:  # noqa: BLE001 - 名字翻不了就用原名，绝不能挡住任务
        if report:
            report("translate", None, f"（文件名没翻成，用原名：{type(e).__name__}）")
        return video.stem
    new = _clean_name(out)
    if not new or new.casefold() == stem.casefold():
        return video.stem
    _NAME_TRANS[key] = new
    if report:
        report("translate", None, f"文件名翻译：{stem} → {new}")
    return new


def _plan_out_dir(base: Path, video: Path, stamp: str, in_batch: bool,
                  batch_index: int = 0, report=None,
                  name_stem: str = "") -> tuple[Path, str]:
    """给这次任务挑输出目录和成品文件名 —— 保证整条路径不超过 MAX_OUT_PATH

    正常情况目录名就是视频名（批量）/ 视频名-时间戳（单个）；视频名太长或源目录
    太深时，目录换成短名、成品文件名头尾各留一段。返回 (输出目录, 成品文件名主干)。
    name_stem 是「翻译后的文件名」；空 = 用原名。
    """
    stem = (name_stem or "").strip() or video.stem
    # 批量模式：父目录已带批次时间，这里直接用视频名；单个模式带时间戳
    # ⚠️ 递归扫子文件夹之后，**不同子目录里同名是常事**（每一章都有一个 "01 Intro.mp4"），
    #    照原名建目录的话第二条会写进第一条的目录里、把结果覆盖掉 —— 撞名就加 -2/-3。
    # ⚠️ 整条路径要压在 MAX_OUT_PATH 以内：Windows 的 MAX_PATH 是 260，超过之后
    #    不认长路径的程序（实测 PotPlayer）会一直卡在「即将播放」打不开。
    def _fits(base_str: str, dir_name: str, stem: str) -> bool:
        return (len(base_str) + 1 + len(dir_name) + 1 + len(stem)
                + len("_dubbed.mp4") <= MAX_OUT_PATH)

    dir_name = stem if in_batch else f"{stem}-{stamp}"
    if not _fits(str(base), dir_name, stem):
        dir_name = (f"{batch_index:03d}" if batch_index else "task") if in_batch else stamp

    budget = (MAX_OUT_PATH - len(str(base)) - 1 - len(dir_name) - 1
              - len("_dubbed.mp4"))
    out_stem = _fit_stem(stem, budget)
    if budget < 8:
        # 源目录本身太深，连最短的名字都放不下 —— 成品改存兜底目录
        alt = _default_output_dir()
        if _writable(alt):
            if report:
                report("translate", None,
                       f"原目录路径太长（接近 Windows 的 260 字符上限），成品改存到 {alt}")
            base = alt
            budget = (MAX_OUT_PATH - len(str(base)) - 1 - len(dir_name) - 1
                      - len("_dubbed.mp4"))
            out_stem = _fit_stem(stem, budget)

    out_dir = base / dir_name
    if in_batch:
        n = 2
        while out_dir.exists():
            out_dir = base / f"{dir_name}-{n}"
            n += 1
    return out_dir, out_stem


def _run_pipeline(params: dict, job: Job, report) -> dict:
    """执行完整流水线，返回 {video, srt, dubbed, final, out_dir}"""
    source = params["source"].strip()
    _precheck(params, report)
    base = resolve_output_base(params, report)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    in_batch = bool(params.get("_in_batch"))

    # 输入：URL 先下载到临时目录；本地文件直接用
    tmp_dl = None
    _ck().enter("下载")
    if re.match(r"^https?://", source, re.I):
        tmp_dl = base / "_下载中"
        report("download", 0, f"开始下载: {source}")
        video = download(source, tmp_dl, params, job, report)
        # 字幕单独拉一次（best-effort）：它失败不能连累已经下好的视频，见 fetch_subs
        if not params.get("download_only"):
            fetch_subs(source, tmp_dl, params, report)
    else:
        video = Path(source)
        if not video.exists():
            raise FileNotFoundError(f"视频文件不存在: {video}")
        report("download", _map("download", 1.0), f"本地视频: {video.name}")
    _ck().leave()

    # 记下原始标题：下载完的视频会被改名成「原始视频」，翻译前的 DeepSeek 查证还要用它搜索
    params["_video_title"] = video.stem
    # 封面要在移动视频之前认：yt-dlp 把封面存在下载目录、和原视频同名，
    # 视频搬进任务目录后（改叫「原始视频.mp4」）就按名字找不到了。
    thumb_src = _find_thumbnail(video)

    name_stem = _translated_stem(video, params, report)
    out_dir, out_stem = _plan_out_dir(base, video, stamp, in_batch,
                                      int(params.get("_batch_index") or 0), report,
                                      name_stem=name_stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 把下载的原片收进任务目录（一个视频 = 一个文件夹，删起来干净）
    if tmp_dl is not None:
        dest = out_dir / f"原始视频{video.suffix}"
        try:
            try:
                video.replace(dest)          # 同盘 move，瞬间完成
            except OSError:
                shutil.move(str(video), str(dest))   # 兜底目录可能在别的盘
            video = dest
            # 字幕跟着一起搬：翻译那一步要靠它跳过语音识别（文件名必须对上视频名）
            _stash_youtube_subs(tmp_dl, out_dir, video.stem, report)
            try:
                tmp_dl.rmdir()
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            report("download", None, f"（原始视频保留在 {tmp_dl}：{e}）")

    report("translate", 0, f"输出目录: {out_dir}")

    # 封面（链接下载时 yt-dlp 会带下来）复制进输出目录
    cover = ""
    thumb = thumb_src or _find_thumbnail(video)
    if thumb:
        try:
            cover_name = (f"原始视频{thumb.suffix}" if params.get("download_only")
                          else f"封面{thumb.suffix}")
            dest = out_dir / cover_name
            dest.write_bytes(thumb.read_bytes())
            cover = str(dest)
            report("download", None, f"封面已保存: {dest.name}")
        except Exception:
            pass

    if params.get("download_only"):
        # 「下载视频」：链接已经下好并收进任务目录；本地视频复制一份进去。
        # 不跑翻译 / 配音 / 压制，也不需要任何 API Key。
        dest = out_dir / f"原始视频{video.suffix}"
        if video.resolve() != dest.resolve():
            if not dest.is_file() or dest.stat().st_size != video.stat().st_size:
                shutil.copy2(video, dest)
            video = dest
        report("done", 100, "🎉 视频已下载" if tmp_dl is not None else "🎉 视频已输出")
        return {"video": str(video), "srt": "", "dubbed": "", "final": "",
                "out_dir": str(out_dir), "cover": cover}

    if params.get("asr_only"):
        # 「只提取原文字幕」：引擎只跑到「原始字幕」（人声分离 + ASR + 稳定化），
        # **一个 LLM 都不调**，不花 DeepSeek 额度。2026-10-22 客户需求。
        _ck().enter("提取原文字幕")
        raw = translate(video, params, job, report, stage="raw-srt")
        src_copy = out_dir / f"{out_stem}.srt"
        src_copy.write_bytes(raw.read_bytes())
        _ck().leave()
        report("done", 100, f"🎉 原文字幕已提取：{src_copy.name}")
        return {"video": str(video), "srt": str(src_copy), "dubbed": "", "final": "",
                "out_dir": str(out_dir), "cover": cover}

    _ck().enter("翻译（引擎）")
    srt = translate(video, params, job, report)
    _ck().leave()
    # 把成品字幕放进输出目录
    srt_copy = out_dir / f"{out_stem}.srt"
    srt_copy.write_bytes(srt.read_bytes())
    src_raw = _work_dir(video, params) / f"{video.stem}-raw.srt"
    srt_copy = _review_final_subtitles(srt_copy, params, job, report,
                                       src_raw if src_raw.is_file() else None)

    # 开了「双语字幕」时，额外导出一份带原文的 SRT 给人工校对用。
    # ★ 2026-10-21 客户需求：批量翻生肉后要对着原文改译文。
    #   ⚠️ 只多写 `_双语.srt`，上面那份纯中文的 `{out_stem}.srt` 不动 ——
    #      配音和烧录都读它，混进原文会把原文也念出来。
    bilingual_srt = ""
    if params.get("subtitle_bilingual"):
        if src_raw.is_file():
            try:
                bi = out_dir / f"{out_stem}_双语.srt"
                n = srt2ass.write_bilingual_srt(
                    srt_copy, src_raw, bi,
                    src_order=str(params.get("subtitle_bilingual_order") or "src_top"))
                bilingual_srt = str(bi)
                report("translate", None, f"双语字幕文件已导出：{bi.name}（{n} 条）")
            except Exception as e:  # noqa: BLE001 - 多一份文件失败不影响正片
                report("translate", None,
                       f"⚠ 双语字幕文件没导出来（{type(e).__name__}: {e}）")
        else:
            report("translate", None, "⚠ 没找到原文对照字幕，这次只导出中文字幕")

    result = {"video": str(video), "srt": str(srt_copy), "dubbed": "", "final": "",
              "out_dir": str(out_dir), "cover": cover,
              "bilingual_srt": bilingual_srt}
    if not params.get("do_dub", True):
        if params.get("burn_subs"):
            # 「只加字幕（保留原声）」：不配音，直接把字幕烧到原视频上，音轨原样 copy
            final = out_dir / f"{out_stem}_final.mp4"
            _ck().enter("压制字幕")
            burn(video, srt_copy, final, job, report, params,
                 src_srt=src_raw if src_raw.is_file() else None)
            result["final"] = str(final)
            report("burn", 100, f"完成: {final.name}")
        else:
            report("dub", 100, "已跳过配音（只出字幕）")
            report("burn", 100, "已跳过烧字幕")
        return result

    dubbed = out_dir / f"{out_stem}_dubbed.mp4"
    fast = _is_fast(params)
    # 音色用云端（MiMo，默认）还是本机（IndexTTS-2.5）复刻 —— 后者要先在设置里下载
    engine = str(params.get("clone_engine") or "local")
    clone_on = bool(params.get("tts_clone"))
    # 快速模式里克隆只有本机（云端 MiMo 不参与）
    clone_local = clone_on and engine == "local"
    qwen_local = clone_on and engine == "qwen"
    if clone_local:
        import localclone
        st = localclone.status()
        if not st["ready"]:
            why = f"：{st['problem']}" if st.get("problem") else ""
            report("dub", None,
                   f"⚠ 本地语音克隆还没装好{why}，这次改用"
                   + ("微软配音（设置 → 配音 → 本地语音克隆 可以接着下）"
                      if fast else
                      "云端克隆（要接着下：设置 → 配音 → 本地语音克隆）"))
            clone_local = False
    if qwen_local:
        import qwenvoice
        stq = qwenvoice.status()
        if not stq["ready"]:
            report("dub", None,
                   "⚠ Qwen3-TTS 还没装好，这次改用"
                   + ("微软配音" if fast else "云端克隆")
                   + "（设置 → 配音 → Qwen3-TTS（本地）可以下载）")
            qwen_local = False

    _ck().enter("配音")
    if qwen_local:
        report("dub", _map("dub", 0), "开始本机 Qwen3-TTS 克隆配音…")
        clone_ref = _clone_reference(video, params, out_dir, report)
        if clone_ref is None:
            qwen_local = False
    elif clone_local:
        # 本地克隆优先：快速模式也可以只用克隆（不分离 + Google 机翻，只是配音慢）
        report("dub", _map("dub", 0), "开始本机语音克隆配音...")
        clone_ref = _clone_reference(video, params, out_dir, report)
        if clone_ref is None:
            clone_local = False
    elif fast:
        # 快速模式不用 MiMo；微软音色代替
        clone_local, clone_ref = False, None
        report("dub", _map("dub", 0), "开始快速模式配音（微软 edge-tts）…")
    else:
        report("dub", _map("dub", 0), "开始 MiMo 配音合成...")
        clone_ref = _clone_reference(video, params, out_dir, report)
        if clone_ref is None:
            clone_local = False
    # 源语言字幕：配音要靠它判断「字幕这一行的换行处是不是句子边界」，
    # 从而把被切开的半句话并成一次合成（见 dubber._cont_flags —— 一行一次合成
    # 会让模型把半句话按整句收尾，听着就是一句一句往外蹦）。
    # ⚠️ 用 `-raw.srt` 别用 `-corrected.srt` —— raw 的结束时间与中文成品逐行精确对齐
    #    （实测 36 行误差 0.000 秒），corrected 差到 0.6 秒、映射不可靠。
    src_srt = _work_dir(video, params) / f"{video.stem}-raw.srt"
    if not src_srt.exists():
        report("dub", None, "（没找到源语言字幕，这次不合并被切开的句子）")
        src_srt = None
    # 配音缓存放**全局持久目录**（不是本任务输出目录下的 tts_cache —— 那个目录名带
    # 时间戳、每次跑都是新的，缓存从来没命中过，同一视频重跑就是全量重新合成）。
    # 整体语速：auto = 只按每句时长自动适配；5 / 10 = 在自动适配之前先整体 +5% / +10%
    _spd = str(params.get("tts_speed") or "auto")
    speed_bias = {"5": 1.05, "10": 1.10}.get(_spd, 1.0)
    _en_out = (params.get("source_lang") or "") == "zh-en"
    _style = params.get("tts_style") or (dubber.DEFAULT_STYLE_EN if _en_out
                                         else dubber.DEFAULT_STYLE)
    _dub_t0 = time.monotonic()
    stats = dubber.dub(srt_copy, video, dubbed,
                       api_key=str(params.get("mimo_key") or ""),
                       voice=params.get("voice", "冰糖"), job=job,
                       orig_vol=float(params.get("orig_vol", 0.0) or 0.0),
                       style=_style,
                       speed_bias=speed_bias,
                       clone_ref=clone_ref, clone_local=clone_local,
                       qwen_local=qwen_local, qwen_voice="",
                       edge=fast and not clone_local and not qwen_local,
                       edge_voice=str(params.get("edge_voice") or dubber.EDGE_DEFAULT),
                       params=params,
                       cache_dir=dubber.default_cache_dir(), src_srt=src_srt,
                       progress_cb=lambda d, t: report(
                           "dub", _map("dub", d / max(1, t)),
                           _dub_eta_text(d, t, _dub_t0)))
    _spd_note = (f"，整体语速 +{int(round((speed_bias - 1) * 100))}%"
                 if speed_bias > 1.001 else "")
    report("dub", _map("dub", 1.0),
           f"配音完成：{stats['segments']} 句"
           f"（{stats.get('merged', 0)} 处被切开的半句并回了整句，"
           f"{stats.get('resynth', 0)} 句请模型说快了一档，"
           f"{stats['fitted']} 句加速、{stats.get('slowed', 0)} 句放慢以填空档{_spd_note}）")
    result["dubbed"] = str(dubbed)
    _ck().leave()

    if params.get("burn_subs", True):
        final = out_dir / f"{out_stem}_final.mp4"
        _ck().enter("压制字幕")
        burn(dubbed, srt_copy, final, job, report, params, src_srt=src_srt)
        result["final"] = str(final)
        report("burn", 100, f"完成: {final.name}")
    return result
