#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内置术语词条：随包带一份，装完引擎后导入用户的词条库（离线、幂等）

**为什么要有**：引擎的「术语知识库」是**每台机器各存一份**的用户数据
（`%LOCALAPPDATA%\\FineSub\\user-data\\knowledge\\`），用户自己那份一开始是空的 ——
模型只能靠常识，不知道「这个软件官方中文叫什么、ASR 常把它听错成什么」。
这份内置词条给所有用户打底（UE / Blender / Houdini / GPT …），
之后引擎每次翻译学到的还会往上加（见 pipeline 的 `--knowledge update`）。

**词条从哪来**：`builtin_knowledge/*.md` —— 就是 `finesub knowledge show` 的原文。
实测**逐字回灌无损**：内容没变时 `edit` 会报 "no change"，所以我们改了词条、
把 `VERSION` +1，用户下次启动就自动拿到新版；`new` 对已存在的条目会自己跳过。

**走官方 CLI，不碰 sqlite**（库是带版本的双时态结构，手写会破坏一致性）：
    finesub knowledge new  common <名字> --type 其他   # 已有则 skipped
    finesub knowledge edit <名字> --file <文件>        # 整条替换
    finesub knowledge refresh                          # 重建 rendered/ 索引（模型读的就是它）

版本记在 `%APPDATA%\\StarryVT\\builtin_kb.json`：一样就一个命令都不跑。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import diag
import settings as cfg
import tools
from job import no_window_kwargs

#: 词条内容有改动就把这个 +1（用户端据此重导一遍）
#: 2026-10-17 +1：新增 MCP 词条
VERSION = 2

_SEED_DIR_NAME = "builtin_knowledge"
_MARKER = cfg.APP_DIR / "builtin_kb.json"
_TYPE = "其他"                      # 这批全是「其他」（见 index.md 的 [其他]）


# ---------- 位置 ----------

def seed_dir() -> Path | None:
    """随包的词条目录（打包后在程序目录 / `_internal` 下；开发时就是仓库根）"""
    d = tools.bundle_dir() / _SEED_DIR_NAME
    return d if d.is_dir() else None


def entries() -> list[tuple[str, Path]]:
    """[(条目名, 文件)] —— 按文件名排序，顺序稳定（日志好读、也便于复现）"""
    d = seed_dir()
    if d is None:
        return []
    return [(p.stem, p) for p in sorted(d.glob("*.md"))]


def _done_version() -> int:
    try:
        return int(json.loads(_MARKER.read_text(encoding="utf-8")).get("version") or 0)
    except Exception:  # noqa: BLE001 - 读不出来就当没导过
        return 0


def _mark(count: int) -> None:
    try:
        cfg.APP_DIR.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(json.dumps(
            {"version": VERSION, "count": count, "at": int(time.time())},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 写不进去只影响「下次还会重导」，不算错
        diag.write(f"[kb] 版本标记写不进去：{e}", "run")


def reset_marker() -> None:
    """抹掉「已导入」标记 → 下次启动（`_seed_builtin_kb`）会重新导一遍。

    给「清空术语库」当兜底：清空之后要是词条没能导回来，标记留着就是一句谎话 ——
    下次启动直接跳过，用户就一直是一本空库（见 knowledge.clear 的复核那一段）。
    """
    try:
        _MARKER.unlink(missing_ok=True)
    except OSError as e:
        diag.write(f"[kb] 版本标记删不掉：{e}", "run")


# ---------- 导入 ----------

def _intro_of(path: Path) -> str:
    """取词条的第二行当 intro（`new` 那步要一个；随后 edit 会整条覆盖，仅作兜底）"""
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        for ln in lines[1:]:
            if ln and not ln.startswith(("#", "<!--")):
                return ln[:120]
    except OSError:
        pass
    return ""


def _run(cmd: list[str], env: dict, job) -> tuple[int, str]:
    """跑一条 CLI。（走 job 时它自己就不弹窗/不给 stdin 了）
    ⚠️⚠️ 不走 job 的分支**必须补 no_window_kwargs()**：里面那条 `stdin=DEVNULL` 是关键 ——
       实测（2026-10-16）在打包后的后端里用裸 `subprocess.run`，子进程会**继承 RPC 管道
       当 stdin** 而卡死不退（`finesub knowledge new …` 挂 25 秒以上）；从命令行跑源码
       却完全正常，正是 job.py 里那个「换个启动方式就复现不了」的老坑。
    """
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if job is not None:
        r = job.run(cmd, **kw)
    else:
        r = subprocess.run(cmd, timeout=180, **kw, **no_window_kwargs())
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def ensure(params: dict | None = None, job=None, report=None, *,
           force: bool = False, root: str | None = None) -> dict:
    """把内置词条导进用户的词条库。幂等、离线；失败绝不抛（只是"这次没导上"）。

    params  用来拿 finesub_exe / finesub_home（和跑引擎时同一套设置）
    root    覆盖知识库位置（默认让 CLI 自己按 FINESUB_HOME 解析；排查/测试用）
    返回    {"ok": bool, "imported": n, "count": 总数, "skipped": 原因}
    """
    params = params or {}
    d = seed_dir()
    items = entries()
    if not d or not items:
        return {"ok": False, "imported": 0, "count": 0, "skipped": "没有随包词条"}
    if not force and _done_version() >= VERSION:
        return {"ok": True, "imported": 0, "count": len(items), "skipped": "已是最新"}

    # ★ 运行环境没装好时**一条命令都别跑**（2026-10-23 客户报障）：
    #   每条 `finesub knowledge ...` 都会重新走 ensure_ready → 发现 ffmpeg 缺失 →
    #   打 GitHub API 要摘要。18 条约 37 次调用，未认证配额是 60 次/小时、**按出口 IP**，
    #   一轮就能烧光；下一轮 `finesub setup` 于是必然撞限流，用户看到的是
    #   「运行环境一直下不下来」，反复重开只会更快耗尽。
    #   词条是锦上添花，等运行环境好了下次启动会自动补（见 winui_backend._seed_builtin_kb）。
    import datadir
    home = (str(params.get("finesub_home") or "").strip()
            or str(datadir.default_home()))
    if not datadir.runtime_ready(Path(home)):
        return {"ok": False, "imported": 0, "count": len(items),
                "skipped": "引擎运行环境还没准备好，先不导入"}

    import engine                                    # 延迟导入：engine 会反过来调本模块
    finesub = engine.find_finesub(str(params.get("finesub_exe") or ""))
    if not finesub:
        return {"ok": False, "imported": 0, "count": len(items), "skipped": "引擎还没装"}

    env = os.environ.copy()
    if params.get("finesub_home"):
        env["FINESUB_HOME"] = str(params["finesub_home"])
    base = [finesub, "knowledge"]
    if root:
        base += ["--root", str(root)]

    if report:
        report("engine", None, f"导入内置术语词条（{len(items)} 条，离线）…")

    done = failed = 0
    stopped = False
    for i, (name, path) in enumerate(items, 1):
        if job is not None:
            try:
                job.check()                          # 用户点了停止就立刻收手
            except Exception:  # noqa: BLE001
                stopped = True
                break
        code, out = _run(base + ["new", "common", name, "--type", _TYPE,
                                 "--intro", _intro_of(path)], env, job)
        if code != 0 and "already exists" not in out and "skipped" not in out:
            failed += 1
            diag.write(f"[kb] 建条目失败 {name}: {out[:200]}", "run")
            continue
        code, out = _run(base + ["edit", name, "--file", str(path)], env, job)
        if code != 0:
            failed += 1
            diag.write(f"[kb] 写词条失败 {name}: {out[:200]}", "run")
            continue
        done += 1
        if report:
            report("engine", None, f"  词条 {i}/{len(items)}：{name}"
                                   + ("（无变化）" if "no change" in out else ""))

    if done:
        _run(base + ["refresh"], env, job)           # 重建索引（模型读的就是它）

    if failed and not done:
        if report:
            report("engine", None, f"⚠ 内置词条一条都没导上（{failed} 条失败，细节在日志）")
        return {"ok": False, "imported": 0, "count": len(items), "skipped": "全部失败"}

    # ⚠️ 被用户中途停掉的**不写版本标记** —— 否则剩下的条目再也不会补上
    #（下次启动的兜底会重跑一遍；已导的条目内容没变，引擎自己报 "no change"）
    if not root and not stopped:
        _mark(len(items))
    elif stopped and report:
        report("engine", None, f"内置词条导到一半停下了（{done}/{len(items)}），下次会接着补")
    if report:
        report("engine", None, f"内置术语词条已就绪：{done}/{len(items)} 条"
                               + (f"，{failed} 条失败" if failed else ""))
    return {"ok": True, "imported": done, "count": len(items),
            "skipped": "" if done else "没变化"}


if __name__ == "__main__":                            # 手动跑：python builtin_kb.py [--root 目录]
    import sys

    argv = sys.argv[1:]
    r = None
    for a in argv:
        if a.startswith("--root="):
            r = a.split("=", 1)[1]
    res = ensure(force=True, root=r, report=lambda stage, pct, msg: print(msg, flush=True))
    print(res)
