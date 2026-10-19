#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机术语库：列出来给用户看 / 清空（「设置 → 高级 → 查看术语库」那个窗口用）

**只读引擎自己的投影，不碰 sqlite**（库是带版本的双时态结构，手写会破坏一致性 ——
见 builtin_kb.py 开头那段）：
  1. 先跑 `finesub knowledge refresh`，把 `rendered/` 跟 sqlite 对齐（实测 0.7 秒）
  2. 再读 `rendered/<域>/index.md` —— 那是引擎自己给人和机读的清单：
         - key [类型] | 其他语言本名 | 别名 | 一句简介

**清空 = 删掉整个知识库目录，再把随包的内置词条重新导一遍 → 回到出厂状态。**
内置词条是随软件发的通用术语（不是从用户视频里学来的），所以保留它们；
界面上的按钮也照这个说，别让用户以为点了还剩东西是没清干净。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import builtin_kb
import diag
from job import no_window_kwargs

#: index.md 的行：`- 虚幻引擎 [其他] | Unreal Engine | UE、UE5 | Epic 的引擎…`
#: ⚠️ 简介里**可以带竖线**（引擎的约定），所以下面只切前两个竖线
_LINE = re.compile(r"^-\s+(?P<name>.+?)\s*\[(?P<type>[^\]]*)\]\s*\|\s*(?P<rest>.*)$")


# ---------- 位置 ----------

def user_data(params: dict) -> Path:
    """个人数据目录（术语库、设置、任务历史都在这里面）。

    ⚠️⚠️ **它不跟着 `FINESUB_HOME` 走** —— 这是引擎刻意的设计（见它自己的
    `finesub_bootstrap/migrations/user_data_location.py`：「个人数据很小又不可再生，
       所以现在只放在一个地方，不管哪个前端打开它」）。实测：
           FINESUB_HOME=D:\\_test\\home 时 doctor 报
               home       D:\\_test\\home
               user-data  C:\\Users\\<你>\\AppData\\Local\\FineSub\\user-data   ← 没跟着走
    ⚠️ 所以**别自己按 finsub_home 拼路径**（2026-10-16 我一开始就是这么写错的：
       设了自定义引擎数据目录的用户，术语库会指向一个不存在的路径、界面显示成空的）。
       这里一律问引擎本人要：跑 `finesub doctor`，解析 `user-data` 那一行。
    拿不到（引擎没装/超时）就退回默认位置。
    """
    import engine
    exe = engine.find_finesub(str(params.get("finesub_exe") or ""))
    if exe:
        try:
            r = subprocess.run([exe, "doctor"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60,
                               **no_window_kwargs())
            for line in (r.stdout or "").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0] == "user-data":
                    return Path(parts[1].strip().split("  (")[0].strip())
        except Exception as e:  # noqa: BLE001 - 问不到就退回默认
            diag.write(f"[kb] 问引擎 user-data 位置失败：{e}", "run")
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "FineSub" / "user-data"


def root_of(params: dict) -> Path | None:
    """知识库目录 = 个人数据目录里的 knowledge/"""
    d = user_data(params) / "knowledge"
    return d if d.is_dir() else None


# ---------- 读 ----------

def _refresh(params: dict) -> str:
    """跑一次 refresh（幂等）。失败不算错 —— 顶多是 rendered/ 稍微旧一点。"""
    import engine
    exe = engine.find_finesub(str(params.get("finesub_exe") or ""))
    if not exe:
        return "没找到引擎"
    env = os.environ.copy()
    if params.get("finesub_home"):
        env["FINESUB_HOME"] = str(params["finesub_home"])
    try:
        r = subprocess.run([exe, "knowledge", "refresh"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           env=env, **no_window_kwargs())
        return "" if r.returncode == 0 else f"refresh 退出码 {r.returncode}"
    except Exception as e:  # noqa: BLE001 - 刷不出来就用现有的
        return f"{type(e).__name__}: {e}"


def _parse_index(path: Path) -> tuple[str, list[dict]]:
    """读一个域的 index.md → (域显示名, 条目列表)"""
    title = path.parent.name
    items: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return title, items
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# "):
            t = line[2:].strip()
            if t.endswith("索引"):
                t = t[:-2].strip()
            if t:
                title = t
            continue
        m = _LINE.match(line)
        if not m:
            continue
        parts = [p.strip() for p in m.group("rest").split("|", 2)]
        while len(parts) < 3:
            parts.append("")
        items.append({
            "name": m.group("name").strip(),
            "type": m.group("type").strip(),
            "native": parts[0],
            "aliases": parts[1],
            "desc": parts[2],
        })
    return title, items


def _count_entries(root: Path) -> int:
    r = root / "rendered"
    if not r.is_dir():
        return 0
    n = 0
    for idx in r.glob("*/index.md"):
        n += len(_parse_index(idx)[1])
    return n


def listing(params: dict) -> dict:
    """给界面用：按域分组的条目 + 总量统计。绝不抛。"""
    warn = _refresh(params)
    root = root_of(params)
    builtin = {name for name, _ in builtin_kb.entries()}

    domains = []
    if root is not None:
        r = root / "rendered"
        if r.is_dir():
            for d in sorted([p for p in r.iterdir() if p.is_dir()], key=lambda p: p.name):
                idx = d / "index.md"
                if not idx.is_file():
                    continue
                title, items = _parse_index(idx)
                for it in items:
                    it["builtin"] = it["name"] in builtin
                domains.append({"id": d.name, "title": title, "entries": items})

    total = sum(len(d["entries"]) for d in domains)
    learned = sum(1 for d in domains for e in d["entries"] if not e["builtin"])
    return {
        "ok": True,
        "domains": domains,
        "total": total,
        "learned": learned,
        "builtin": total - learned,
        "path": str(root) if root else "",
        "warn": warn,
    }


# ---------- 清空 ----------

def clear(params: dict, job=None, root: Path | None = None) -> dict:
    """清空知识库 → 重新导入内置词条。返回 {ok, removed, restored, error, note}

    root 只在**测试**时用（沙箱里指一个假目录）；生产一律走 root_of(params)。
    """
    kb = root or root_of(params)

    if kb is None:
        r = builtin_kb.ensure(params, job, None, force=True)
        return {"ok": True, "removed": 0, "restored": r.get("imported", 0)}

    # 安全闸：只认「名字叫 knowledge 而且里面真有库文件」的目录 —— 路径是从引擎的
    # doctor 输出推出来的，删错地方（比如引擎哪天换了布局）代价太大，宁可停下报错
    if kb.name != "knowledge" or not (kb / "knowledge.sqlite").exists():
        return {"ok": False, "error": f"这个位置不像是术语库，没有动它：{kb}"}

    busy = []
    try:
        import engine
        busy = engine.busy_processes()
    except Exception:  # noqa: BLE001 - 查不出来就不拦
        pass
    if busy:
        return {"ok": False, "error": "翻译引擎正在运行（" + "、".join(busy) + "），先等它跑完再清空"}

    removed = _count_entries(kb)
    try:
        shutil.rmtree(kb)
    except Exception as e:  # noqa: BLE001
        diag.write_exception(e, "清空术语库")
        return {"ok": False, "error": f"删不掉：{type(e).__name__}: {e}（可能被别的程序占着）"}

    r = builtin_kb.ensure(params, job, None, force=True)

    # ★ 复核：**真去看一眼库回来了没有**，别只信 ensure 的返回值。
    #   实测（2026-10-16 沙箱）：ensure 会拿「子进程退出码 0」当成功，而引擎在
    #   「发现两处个人数据」这类情况下会**什么也不做还返回 0** —— 于是它报
    #   「导上 17 条」、库里却是空的。真出了这种事，用户看到的是一本空术语库，
    #   而且版本标记已经写上去了、下次启动不会补 → 必须在这儿救回来。
    back = {}
    if root is None:                       # 沙箱里跑的时候别去动真库
        back = listing(params)
    ok = bool(root) or back.get("total", 0) > 0
    if not ok:
        builtin_kb.reset_marker()          # 别留下「已导入」的谎话
        diag.write("[kb] 清空后内置词条没能导回来，已重置标记（下次启动会重导）", "run")
        return {"ok": False, "removed": removed, "restored": 0,
                "error": "已清空，但内置词条没能重新导入 —— 下次启动软件会自动补上"}

    return {
        "ok": True,
        "removed": removed,
        "restored": r.get("imported", 0),
        "note": "" if r.get("ok") else f"内置词条没能重新导入：{r.get('skipped')}",
    }
