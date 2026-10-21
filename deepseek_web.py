#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeepSeek 网页搜索 / 抓取（官方 Search / Fetch API）。

接口：
    POST https://api.deepseek.com/v1/tools/search
    POST https://api.deepseek.com/v1/tools/fetch
鉴权与 DeepSeek 翻译相同：Authorization: Bearer <deepseek_key>。

只做薄封装：Key 从 settings 里读，不写日志；返回 {ok, ...}，不抛异常。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "https://api.deepseek.com"


def _key(key: str | None = None) -> str:
    if key:
        return key.strip()
    import settings as cfg
    return str(cfg.load().get("deepseek_key") or "").strip()


def _post(path: str, payload: dict, key: str = "", timeout: float = 60) -> dict:
    import mirrors
    import settings as cfg
    params = cfg.load()
    k = (key or str(params.get("deepseek_key") or "")).strip()
    if not k:
        return {"ok": False, "error": "未配置 DeepSeek API Key"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {k}",
        },
    )
    try:
        # 有可用代理就走，没有就强制直连（系统代理指着死端口时别被它带崩）
        with mirrors.opener(params).open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:500]
        msg = raw
        try:
            msg = (json.loads(raw).get("error") or {}).get("message") or raw
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"DeepSeek 接口失败（HTTP {e.code}）：{msg}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"DeepSeek 接口失败：{type(e).__name__}: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "DeepSeek 接口返回格式不正确"}
    return {"ok": True, **data}


def search(query: str, count: int = 10, need_content: bool = False,
           ori_question: str = "", localization: str = "",
           key: str = "", timeout: float = 30) -> dict:
    """搜索网页。返回 results：[{title,url,snippet,summary,content,...}]。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空"}
    payload = {
        "query": q[:128],
        "count": max(1, min(int(count or 10), 50)),
        "need_content": bool(need_content),
    }
    if ori_question:
        payload["ori_question"] = ori_question[:128]
    if localization:
        payload["localization"] = localization
    return _post("/v1/tools/search", payload, key, timeout)


def fetch(url: str, key: str = "", timeout: float = 60) -> dict:
    """抓取单个网页。返回 {title, content(Markdown)}。"""
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "只支持 http / https 链接"}
    return _post("/v1/tools/fetch", {"url": u[:8192]}, key, timeout)


if __name__ == "__main__":
    import json as _json
    import sys
    r = search(sys.argv[1] if len(sys.argv) > 1 else "DeepSeek V4", count=3)
    print(_json.dumps({k: v for k, v in r.items() if k != "results"}, ensure_ascii=False))
    for it in (r.get("results") or [])[:3]:
        print(it.get("rerank_score"), it.get("title"), it.get("url"))
