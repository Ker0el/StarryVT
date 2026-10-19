#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WinUI 前端用的后端：**JSON-RPC over stdio**。

前端（C# WinUI 3）把这个进程拉起来，按行发 JSON：

    {"id": 1, "method": "boot", "params": {}}

回来：

    {"id": 1, "ok": true, "result": {...}}

另外会**主动推**任务状态（前端不用轮询）：

    {"event": "status", "status": {...}}

为什么走 stdio 而不是本地 HTTP：不用挑端口（这台机器 13239-13338 那段被 Hyper-V 圈走了，
连 13337 都绑不上）、没有防火墙弹窗、父进程一死子进程跟着结束。

复用：直接 import app 里的 Api —— pipeline/引擎/配音/更新器**一行都不用改**。
只有 pick_video / pick_dir 依赖 pywebview 的窗口，这两个由前端自己弹系统文件对话框，
这里返回错误即可。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

# ⚠️⚠️ Windows 上 Python 往**管道**写中文默认用系统代码页（这台机器是 GBK），
#     而前端按 UTF-8 读 → 每个中文字节都成了非法序列 → U+FFFD，界面上一排 ◆。
#     实测：`"summary":"一切正常"` 出去是 \xd2\xbb\xc7\xd0\xd5\xfd\xb3\xa3（GBK）。
#     两头都在这儿钉死成 UTF-8，别指望环境变量（打包后未必有）。
for _stream in (sys.stdin, sys.stdout):
    try:
        # stdin 用 utf-8-sig：万一前端还是发了带 BOM 的 UTF-8（.NET 的 Encoding.UTF8
        # 就会），这里顺手把 BOM 吃掉，而不是回一句「这行不是 JSON」让前端干等超时。
        _stream.reconfigure(encoding="utf-8-sig" if _stream is sys.stdin else "utf-8")
    except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure 就随它去
        pass

# ⚠️ 打包后 app.py 是以 `__main__` 跑的（`StarryVT.exe --rpc`），直接 `import app`
#    会把同一个文件再加载一份 —— 两个 Api 类、两份模块级状态，出问题极难查。
#    所以先把已有的那份登记成 `app`。
_main = sys.modules.get("__main__")
if _main is not None and hasattr(_main, "Api") and "app" not in sys.modules:
    sys.modules["app"] = _main

# app 里的 Api 只有 pick_* 用到 window；这里不 attach_window，其余方法照用。
# app.py 顶层 import 了 webview，但只是读几个常量，不需要真的起窗口。
import app as vt_app  # noqa: E402

api = vt_app.Api()

_LOCK = threading.Lock()          # 写 stdout 要串行，否则两行会交叉
_LAST = {"sig": None}


def _send(obj: dict) -> None:
    with _LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _status_pusher() -> None:
    """每 300ms 看一眼任务状态，变了就推给前端（日志一行行涨，不推的话前端看不见）"""
    while True:
        try:
            st = api.get_status()
            # ⚠️ 队列要比**内容**，不能只比长度：批量任务里每个条目的状态是原地改的
            #    （pending→running→done），长度从头到尾不变，只比 len 的话这些变化
            #    一个都推不出去（老界面是每秒轮询 get_status，所以没暴露）。2026-10-14 修。
            q = st.get("queue") or []
            qsig = tuple((it.get("status"), it.get("detail")) for it in q)
            sig = (st.get("running"), st.get("stage"), st.get("percent"),
                   len(st.get("log") or []), bool(st.get("error")), qsig,
                   st.get("out_dir"), st.get("subtitle_raw"),
                   st.get("subtitle_final"),
                   st.get("awaiting_review"), st.get("review_kind"))
            if sig != _LAST["sig"]:
                _LAST["sig"] = sig
                _send({"event": "status", "status": st})
        except Exception:
            pass
        time.sleep(0.3)


def _preview_voice_abs(voice: str) -> dict:
    """试听样音，但返回**绝对路径**。

    网页版拿到的是相对文件名（浏览器按网页目录解析），WinUI 这边要的是完整路径才能播。
    """
    from pathlib import Path

    r = api.preview_voice(voice)
    if r.get("ok") and r.get("file"):
        r["path"] = str((Path(vt_app.WEB_DIR) / r["file"]).resolve())
    return r


def _dispatch(method: str, params: dict):
    if method in ("pick_video", "pick_dir"):
        raise RuntimeError("选文件由前端自己弹对话框，不走后端")
    if method == "preview_voice_abs":
        return _preview_voice_abs(params.get("voice", ""))
    fn = getattr(api, method, None)
    if fn is None or method.startswith("_"):
        raise RuntimeError(f"没有这个方法: {method}")

    # ⚠️⚠️ app.py 里的方法分两派，铺参数的方式不一样：
    #   ① 每个参数对应 JSON 里的一个键：env_report(deep=…)、probe(source=…)、
    #      open_path(path=…)、save_settings(patch=…) → 直接铺开（fn(**params)）
    #   ② 只收一个大 dict：start(params)、start_batch(params)、scan_source(params)
    #      → 必须整个传进去
    #   一开始只写了①，于是点「开始翻译」后端立刻回
    #   `Api.start() got an unexpected keyword argument 'source'`（2026-10-14 实测）。
    #   这里按**签名**判断，别再靠约定：只有一个参数且叫 params 的，走②。
    import inspect

    names = [p for p in inspect.signature(fn).parameters if p != "self"]
    if len(names) == 1 and names[0] == "params":
        return fn(params or {})
    return fn(**params) if params else fn()


def _auto_update() -> None:
    """启动时后台检查更新，查完**主动推给前端**（界面不用轮询）。

    ⚠️⚠️ 这件事以前只写在 `app.py` 的 `main()` 里 —— 那是**老网页界面**的入口，
    WinUI 这条链根本不走它，所以切过来之后「自动检查更新」整个丢了：
    设置页手动查得到，但启动时不查、界面上也没有任何提示。
    2026-10-15 用户发现。**以后凡是「启动时要做的事」，两个入口都要看一眼。**
    """
    try:
        if not vt_app.cfg.load().get("auto_update_check", True):
            return
        info = api.check_update()
        api._update_info = info          # 和 Api.auto_check_update 里填的是同一个东西
        _send({"event": "update", "info": info})
    except Exception as e:  # noqa: BLE001 - 查更新失败绝不能影响启动
        _send({"event": "update", "info": {"ok": False, "has_update": False, "error": str(e)}})


def _seed_builtin_kb() -> None:
    """启动时后台把内置术语词条导进用户的词条库（幂等；版本没变秒回）。

    新用户走「一键初始化」时 `engine.install_finesub` 已经导过一次 —— 这里兜的是
    **老用户**（引擎早就装好了、但还没有随包词条的那批）。不打扰用户，但**结果一定
    写一行日志**（`[kb] …`）：出问题时能一眼看出「导了没有、几条、为什么没导」，
    别再变成又一个查不出来的静默失败。
    """
    import diag
    try:
        import builtin_kb
        r = builtin_kb.ensure(vt_app.cfg.load())
        diag.write(f"[kb] 启动导入：{r}", "run")
    except Exception as e:  # noqa: BLE001 - 词条是锦上添花，绝不能影响启动
        try:
            diag.write(f"[kb] 启动导入异常：{type(e).__name__}: {e}", "run")
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    threading.Thread(target=_status_pusher, daemon=True).start()
    _send({"event": "ready", "version": vt_app.APP_VERSION})
    threading.Thread(target=_auto_update, daemon=True).start()
    threading.Thread(target=_seed_builtin_kb, daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            _send({"event": "warn", "message": f"这行不是 JSON: {e}"})
            continue

        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        try:
            result = _dispatch(method, params)
            _send({"id": mid, "ok": True, "result": result if result is not None else {}})
        except Exception as e:  # noqa: BLE001 - 任何异常都回给前端，别让它干等
            _send({"id": mid, "ok": False, "error": str(e),
                   "trace": traceback.format_exc()[-2000:]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
