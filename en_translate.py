#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中文 → 英语字幕翻译（独立于 finesub 的中文翻译阶段）。

finesub 的翻译提示词写死输出简体中文，所以中文→英语不能走它的翻译阶段：
先用 `--stage raw-srt` 拿到中文时间轴，再由这里调用 DeepSeek 翻成英文。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# DeepSeek 的 OpenAI 兼容接口（和 llmroute.py 里给引擎用的是同一个 base_url）
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
#: 一次请求最多翻多少条字幕（太多会超上下文/变慢，太少调用次数多）
BATCH = 30

SYSTEM = (
    "You are a professional subtitle translator. Translate the Chinese subtitle "
    "lines into natural, concise English suitable for dubbing. Keep names, brands "
    "and technical terms accurate. Keep each line short enough to be read in its "
    "original time slot. Output ONLY a JSON array of strings, one per input line, "
    "in the same order and count. Do not add explanations, notes or markdown."
)


def _post(payload: dict, key: str, timeout: float = 120.0) -> str:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        got = json.loads(r.read().decode("utf-8"))
    return got["choices"][0]["message"]["content"]


def _parse_array(content: str, n: int) -> list[str] | None:
    text = content.strip()
    # 去掉 ```json ... ``` 围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        arr = json.loads(text)
    except Exception:  # noqa: BLE001
        # 有时模型会在数组前后加一句话；截取第一个 [ 到最后一个 ]
        a, b = text.find("["), text.rfind("]")
        if a < 0 or b <= a:
            return None
        try:
            arr = json.loads(text[a:b + 1])
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    return [str(x).strip() for x in arr]


def _translate_batch(lines: list[str], key: str, extra_info: str = "") -> list[str]:
    system = SYSTEM
    if extra_info.strip():
        system += "\nVideo context: " + extra_info.strip()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(lines, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    last = ""
    for attempt in range(4):
        try:
            content = _post(payload, key)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            last = f"HTTP {e.code}: {body}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"DeepSeek 翻译失败：{last}") from e
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (attempt + 1))
            continue
        arr = _parse_array(content, len(lines))
        if arr is not None:
            return arr
        last = "返回的不是等长 JSON 数组"
        # 重试时在提示里再强调一次
        payload["messages"][0]["content"] = system + "\nReturn exactly a JSON array of strings."
    raise RuntimeError(f"DeepSeek 翻译失败：{last}")


def translate_cues(cues, key: str, extra_info: str = "", report=None):
    """把 [(start, end, 中文)] 翻成 [(start, end, English)]，时间轴不动。"""
    out = []
    for i in range(0, len(cues), BATCH):
        chunk = cues[i:i + BATCH]
        texts = [c[2] for c in chunk]
        if report:
            report("translate", None, f"英文翻译 {i + 1}-{i + len(chunk)} / {len(cues)}")
        english = _translate_batch(texts, key, extra_info)
        for (s, e, _zh), en in zip(chunk, english):
            out.append((s, e, en))
    return out


def _ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str) -> str:
    # 字幕保持一行；太长由 srt2ass 按画面宽度自动缩小字号
    return re.sub(r"\s+", " ", text).strip()


def to_srt(cues) -> str:
    blocks = []
    n = 0
    for s, e, text in cues:
        if e <= s:
            e = s + 0.8
        body = _wrap(text)
        if not body:
            continue
        n += 1
        blocks.append(f"{n}\n{_ts(s)} --> {_ts(e)}\n{body}\n")
    return "\n".join(blocks)


def translate_srt(raw_srt: Path, out_srt: Path, key: str,
                  extra_info: str = "", report=None) -> Path:
    """读中文 raw SRT，写英文 SRT，返回输出路径。"""
    import dubber

    cues = dubber.parse_srt(raw_srt)
    if not cues:
        raise RuntimeError("中文识别结果为空，没法翻成英文")
    english = translate_cues(cues, key, extra_info, report)
    out_srt.write_text(to_srt(english), encoding="utf-8")
    if report:
        report("translate", None, f"英文字幕已生成：{len(english)} 条")
    return out_srt
