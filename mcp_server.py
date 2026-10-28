#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""StarryVT 的 MCP 服务器（stdio）。

让 Claude Code / Claude Desktop / Cursor 等 MCP 客户端直接调用星译：
启动翻译任务、查询进度、取消任务、查术语库。

启动方式（开发版）：
    python D:\\AI-Project\\StarryVT\\mcp_server.py

Claude Code 添加（把路径换成自己的仓库位置）：
    claude mcp add --transport stdio starryvt -- python D:\\AI-Project\\StarryVT\\mcp_server.py

设计上只做**薄封装**：实际流水线仍走 app.Api，和 WinUI 用的是同一套设置、API Key、
引擎与输出目录。视频不上传，识别和压制在用户本机完成；翻译和配音用用户自己的
API Key 调用第三方服务。

⚠️ 任务句柄只在**当前 MCP 服务器进程**内有效。MCP 客户端要在这个进程存活期间轮询
starryvt_status；进程退出后，未完成的任务也会随之结束。这是 v1 的已知边界。
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 和 winui_backend 一样：打包后用 `StarryVT.exe --mcp` 启动时，app.py 是 __main__，
# 直接 import app 会再加载一份。把已有的那份登记成 app，保证只用一套 Api / 模块状态。
_main = sys.modules.get("__main__")
if _main is not None and hasattr(_main, "Api") and "app" not in sys.modules:
    sys.modules["app"] = _main

import settings as cfg  # noqa: E402
import app as vt_app  # noqa: E402

# mcp 1.x 的 FastMCP；2.x 把它改名为 MCPServer。这里兼容两种安装，避免跟着
# 系统里别的 MCP 服务一起升级/降级。
try:  # pragma: no cover - 取决于环境里装的是 1.x 还是 2.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from mcp.server import MCPServer as FastMCP  # type: ignore

api = vt_app.Api()

_instructions = (
    "星译（StarryVT）是本机的视频翻译工具，可以把视频或 YouTube / B 站链接翻成中文，"
    "生成字幕、中文配音和成片。视频不上传，识别和压制在用户本机完成；"
    "翻译和配音用用户自己的 API Key 调用第三方服务。\n"
    "开始任务前先确认输入和输出位置；任务会使用用户自己的 API Key，可能产生费用。"
    "用 starryvt_translate 启动，拿到 job_id 后用 starryvt_status 轮询，"
    "或者用 starryvt_wait 等待到完成。一次只跑一个任务。"
)
try:
    mcp = FastMCP("StarryVT", instructions=_instructions, log_level="WARNING",
                  host="127.0.0.1", port=24139)
except TypeError:  # pragma: no cover - v2 的构造函数不一定收这些参数
    mcp = FastMCP("StarryVT", instructions=_instructions)

# ---------- 任务句柄 ----------

_job_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_current: str | None = None
_seq = 0


def _new_job_id() -> str:
    global _seq
    with _job_lock:
        _seq += 1
        return time.strftime("svt-%Y%m%d-%H%M%S") + f"-{_seq}"


def _redact(text: str) -> str:
    """把日志里的 API Key 换成 ***；工具结果会被模型看到，不能带明文密钥。"""
    try:
        c = cfg.load()
    except Exception:  # noqa: BLE001
        return text
    for name in ("mimo_key", "deepseek_key", "gemini_key"):
        key = str(c.get(name) or "")
        if len(key) >= 8:
            text = text.replace(key, "***")
    return text


def _state_of(st: dict) -> str:
    if st.get("running"):
        return "running"
    if st.get("error"):
        return "error"
    if st.get("stage") == "cancelled":
        return "cancelled"
    if st.get("result"):
        return "done"
    return "idle"


def _summary(job_id: str | None, st: dict) -> dict:
    logs = [_redact(str(x)) for x in (st.get("log") or [])[-40:]]
    return {
        "ok": True,
        "job_id": job_id,
        "state": _state_of(st),
        "running": bool(st.get("running")),
        "stage": st.get("stage") or "",
        "percent": int(st.get("percent") or 0),
        "error": _redact(str(st.get("error") or "")),
        "result": st.get("result"),
        "log_tail": logs,
        "queue": st.get("queue") or [],
    }


def _job_status(job_id: str = "") -> dict:
    """当前任务或指定 job_id 的状态；已结束的任务保留最后一次快照。"""
    requested = (job_id or "").strip()
    with _job_lock:
        current = _current
        if not requested:
            if not current:
                return _summary(None, api.get_status())
            requested = current
        if requested != current:
            rec = _jobs.get(requested)
            if rec and rec.get("last"):
                return rec["last"]
            return {"ok": False, "error": f"没有这个任务：{requested}", "state": "unknown"}
        st = api.get_status()
        out = _summary(requested, st)
        if not st.get("running"):
            _jobs[requested]["last"] = out
        return out


def _voice_ok(voice: str) -> bool:
    try:
        return voice in cfg.VOICES
    except Exception:  # noqa: BLE001
        return True


# ---------- 工具 ----------


@mcp.tool()
def starryvt_env() -> dict:
    """查看星译是否就绪：翻译引擎、API Key 是否配置、可用音色和语言。

    这是只读检查；不会启动任务，也不会返回任何 Key 的明文。
    """
    try:
        eng = api.engine_status()
        c = cfg.load()
        st = api.get_status()
        return {
            "ok": True,
            "app_version": vt_app.APP_VERSION,
            "engine_ready": bool(eng.get("ready")),
            "engine_path": eng.get("engine_path") or "",
            "uv_ready": bool(eng.get("uv_ready")),
            "keys": {
                "mimo": bool(c.get("mimo_key")),
                "deepseek": bool(c.get("deepseek_key")),
                "gemini": bool(c.get("gemini_key")),
            },
            "voices": list(cfg.VOICES),
            "langs": [{"id": k, "name": v} for k, v in cfg.LANGS],
            "running": bool(st.get("running")),
            "state": _state_of(st),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}")}


@mcp.tool()
async def starryvt_probe(source: str) -> dict:
    """读取视频标题并猜测背景信息，不会启动翻译。

    Args:
        source: 本地视频路径，或 YouTube / B 站链接。
    """
    source = (source or "").strip()
    if not source:
        return {"ok": False, "error": "source 不能为空"}
    try:
        return await asyncio.to_thread(api.probe, source)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}")}


@mcp.tool()
def starryvt_translate(
    source: str,
    source_lang: Literal["en", "ja", "ko", "ru", "de", "zh-en", "zh"] = "en",
    output_mode: Literal["final", "dub", "subs", "subs-burn", "subs-src", "download"] = "final",
    output_dir: str = "",
    voice: str = "冰糖",
    tts_style: str = "",
    tts_speed: Literal["auto", "5", "10"] = "auto",
    extra_info: str = "",
    batch: bool = False,
    batch_kind: Literal["folder", "playlist"] = "folder",
) -> dict:
    """在本机启动一个星译翻译任务，立即返回 job_id，不会阻塞到任务结束。

    Args:
        source: 本地视频 / 文件夹路径，或 YouTube / B 站链接。
        source_lang: 源语言。en / ja / ko / ru / de 译为中文，zh-en 是中文译为英文，zh 是中文换音色重配。
        output_mode: final = 成片（配音 + 烧录字幕）；dub = 只出配音；subs = 只出字幕（仍会翻译）；subs-src = 只提取原文字幕（不翻译、不花 DeepSeek 额度）；subs-burn = 只烧字幕、保留原声；download = 只下载/输出视频，不翻译。
        output_dir: 输出目录；空 = 视频旁边的 output\\ 或设置里的输出目录。
        voice: 配音音色，必须是 starryvt_env 返回的 voices 之一。
        tts_style: 配音怎么读的自然语言描述；空 = 标准语速。
        tts_speed: auto = 标准语速；5 = 整体 +5%；10 = 整体 +10%。
        extra_info: 给翻译模型的背景信息；可以从 starryvt_probe 的 extra 取。
        batch: 是否批量处理文件夹或合集链接。
        batch_kind: 批量来源类型；文件夹用 folder，YouTube 合集用 playlist。
    """
    source = (source or "").strip()
    if not source:
        return {"ok": False, "error": "source 不能为空"}
    global _current

    is_url = bool(re.match(r"^https?://", source, re.I))
    if not is_url:
        p = Path(source)
        if not p.exists():
            return {"ok": False, "error": f"找不到文件或文件夹：{source}"}
        if p.is_dir():
            batch = True

    if output_mode != "download":
        # 只提取原文字幕不用配音，音色随便填都不该拦；引擎仍然要（ASR 靠它）
        if output_mode != "subs-src" and not _voice_ok(voice):
            return {"ok": False, "error": f"没有这个音色：{voice}",
                    "voices": list(cfg.VOICES)}

        try:
            eng = api.engine_status()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _redact(f"检查引擎失败：{type(e).__name__}: {e}")}
        if not eng.get("ready"):
            return {"ok": False,
                    "error": "翻译引擎还没装好。请先打开星译，在首页点【一键初始化】。"}

    with _job_lock:
        current = _current
        st_now = api.get_status()
        if st_now.get("running"):
            return {"ok": False, "error": "已有任务在运行，先等它结束或调用 starryvt_cancel。",
                    "job_id": current}
        # 上一个任务已经结束但还没人查过状态：把它的最后一帧留档，别让 _current 一直挡着。
        if current and current in _jobs and not _jobs[current].get("last"):
            _jobs[current]["last"] = _summary(current, st_now)

    params = {
        "source": source,
        "source_lang": source_lang,
        "output_dir": output_dir,
        "voice": voice,
        "tts_style": tts_style,
        "tts_speed": tts_speed,
        "extra_info": extra_info,
        # 字幕检查是界面里的暂停点，MCP 调用没有人点「继续」——一律不停，
        # 免得 Agent 的任务卡在等一个不会来的确认。批量本来也不停。
        "subtitle_review": False,
        "default_output": output_mode,
        "do_dub": output_mode in ("final", "dub"),
        "burn_subs": output_mode in ("final", "subs-burn"),
        "download_only": output_mode == "download",
        "asr_only": output_mode == "subs-src",
        "batch_kind": batch_kind,
    }

    job_id = _new_job_id()
    try:
        if batch:
            r = api.start_batch(params)
        else:
            r = api.start(params)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"启动失败：{type(e).__name__}: {e}")}
    if not r.get("ok"):
        return {"ok": False, "error": _redact(str(r.get("error") or "启动失败"))}

    with _job_lock:
        _current = job_id
        _jobs[job_id] = {
            "id": job_id,
            "source": source,
            "output_mode": output_mode,
            "started": time.time(),
            "last": None,
        }
    mode_text = {
        "final": "成片（配音 + 烧录字幕）",
        "dub": "只出配音",
        "subs": "只出字幕",
        "subs-burn": "只烧字幕，保留原声",
        "download": "只下载视频",
    }[output_mode]
    return {
        "ok": True,
        "job_id": job_id,
        "state": "running",
        "mode": mode_text,
        "message": "任务已在本机开始。用 starryvt_status 轮询，或 starryvt_wait 等待完成。",
    }


@mcp.tool()
def starryvt_status(job_id: str = "") -> dict:
    """查询翻译任务的状态、进度、最近日志和输出路径。

    Args:
        job_id: 任务 id；留空 = 当前任务。
    """
    try:
        return _job_status(job_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}")}


@mcp.tool()
async def starryvt_wait(job_id: str = "", timeout_sec: int = 60) -> dict:
    """等待任务结束（最多 timeout_sec 秒），期间每秒查一次状态。

    Args:
        job_id: 任务 id；留空 = 当前任务。
        timeout_sec: 最长等待秒数，1 到 600，默认 60。
    """
    try:
        limit = max(1, min(int(timeout_sec), 600))
    except Exception:  # noqa: BLE001
        limit = 60
    deadline = time.monotonic() + limit
    while True:
        st = _job_status(job_id)
        if not st.get("ok") or not st.get("running"):
            return st
        if time.monotonic() >= deadline:
            st = dict(st)
            st["timed_out"] = True
            st["message"] = f"等待 {limit} 秒后任务还在运行；可以继续调用 starryvt_wait 或 starryvt_status。"
            return st
        await asyncio.sleep(1.0)


@mcp.tool()
def starryvt_cancel(job_id: str = "") -> dict:
    """取消正在运行的翻译任务。

    Args:
        job_id: 任务 id；留空 = 当前任务。
    """
    requested = (job_id or "").strip()
    with _job_lock:
        current = _current
    if requested and requested != current:
        return {"ok": False, "error": f"没有正在运行的该任务：{requested}"}
    if not current:
        return {"ok": False, "error": "当前没有运行中的任务"}
    try:
        if not api.get_status().get("running"):
            return {"ok": False, "error": "当前没有运行中的任务", "job_id": current}
        api.cancel()
        return {"ok": True, "job_id": current, "message": "已发出取消请求，任务会在下一步停止。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}")}


@mcp.tool()
async def starryvt_knowledge_search(query: str = "", limit: int = 20) -> dict:
    """搜索本机术语库，返回条目名称、别名、类型和简介。

    Args:
        query: 关键词；空 = 列出前 limit 条。
        limit: 最多返回多少条，1 到 100，默认 20。
    """
    try:
        n = max(1, min(int(limit), 100))
    except Exception:  # noqa: BLE001
        n = 20
    try:
        listing = await asyncio.to_thread(api.knowledge_list)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _redact(f"{type(e).__name__}: {e}")}
    if not listing.get("ok"):
        return {"ok": False, "error": _redact(str(listing.get("error") or "读取术语库失败"))}
    needle = (query or "").strip().lower()
    hits = []
    for dom in listing.get("domains") or []:
        for it in dom.get("entries") or []:
            blob = " ".join(str(it.get(k) or "") for k in ("name", "native", "aliases", "desc", "type"))
            if needle and needle not in blob.lower():
                continue
            hits.append({
                "name": it.get("name") or "",
                "domain": dom.get("id") or "",
                "type": it.get("type") or "",
                "native": it.get("native") or "",
                "aliases": it.get("aliases") or "",
                "desc": it.get("desc") or "",
                "builtin": bool(it.get("builtin")),
            })
            if len(hits) >= n:
                break
        if len(hits) >= n:
            break
    return {
        "ok": True,
        "query": query or "",
        "total": listing.get("total", 0),
        "learned": listing.get("learned", 0),
        "builtin": listing.get("builtin", 0),
        "hits": hits,
    }


def main(http: bool = False) -> None:
    # 直接 `python mcp_server.py` 时也要挂进程守卫：MCP 客户端关掉服务器时，
    # 引擎子进程不能变成孤儿继续占着引擎数据目录的锁。
    try:
        from job import guard as guard_processes
        guard_processes()
    except Exception:  # noqa: BLE001 - 守卫失败不影响 MCP 本身
        pass
    if http:
        # 只给「客户端只收 HTTP」的情况用（例如部分豆包工作版本）。
        # 绑定 127.0.0.1，不暴露到局域网；地址是 http://127.0.0.1:24139/mcp。
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main(http="--http" in sys.argv or "--mcp-http" in sys.argv)
