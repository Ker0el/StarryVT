#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""搬引擎数据目录：把整个数据目录挪到别的盘（先搬；搬不动就退回「在新位置重新准备」）

**为什么要有**：换目录以前只有一条路 —— 在新位置把 3.7GB 模型 + 2.6GB 运行环境
重新准备一遍（设置页那个确认框写的「换 = 重下」）。而这个目录里绝大多数是**文件**，
同盘搬家只是改个名（瞬时），跨盘也就是复制几分钟，比重新下载快得多。
2026-10-16 用户定：「先搬；搬不动、或搬完自检不过 → 自动回退重下」。

**搬什么**：整个数据目录（runtime + models + cache + tasks）。
venv 里那些"写死的绝对路径"实测不挡事 —— 把 runtime 复制到别的盘、直接用那个盘的
python 跑 torch/ctranslate2/faster_whisper 全部正常（见项目记忆；引擎自己的
`finesub relocate` 只搬 models/cache/tasks、故意不动 runtime，理由是跨盘会让
uv 的硬链缓存多占一份 5GB —— 我们整份搬，所以没有这个代价）。

**自检**：搬完拿新位置的 runtime 真跑一次 `finesub doctor`，要看到 runtime=ready
且模型在位才算数；不过就把新目录清掉、原处一个字不动（返回 ok=False），
由调用方决定是"就这样"还是"在新位置重下"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from job import Job, no_window_kwargs
import datadir
import engine
import tools

#: 判定"这个目录里已经装好了"的锚点（和 datadir.runtime_ready 一致）
_ANCHOR = Path("runtime") / "python" / "Scripts" / "python.exe"

#: 复制时每这么多字节报一次进度
_STEP = 256 << 20


def _size_of(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(str(a.resolve()))[0].lower() == \
               os.path.splitdrive(str(b.resolve()))[0].lower()
    except OSError:
        return False


def _check_target(src: Path, dst: Path) -> str:
    """返回一句人话的拒绝理由；可以搬就返回空串"""
    try:
        s, d = src.resolve(), dst.resolve()
    except OSError as e:
        return f"路径解析不了：{e}"
    if s == d:
        return "新位置就是现在的位置，不用搬"
    if s in d.parents:
        return "新位置不能放在数据目录里面"
    if d in s.parents:
        return "数据目录不能放进新位置里面"
    if d.exists() and any(d.iterdir()):
        return f"新位置不是空的：{d}（挑一个空文件夹或新建一个）"
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        probe = d.parent / ".starryvt-move-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return f"新位置写不进去：{type(e).__name__}"
    need = _size_of(src)
    try:
        free = shutil.disk_usage(d.parent).free
    except OSError:
        return ""
    if free < need + (512 << 20):
        return (f"那个盘空间不够：要 {need / 1073741824:.1f} GB，"
                f"只剩 {free / 1073741824:.1f} GB")
    return ""


def _copy_tree(src: Path, dst: Path, job: Job, report) -> None:
    """跨盘复制（有进度）。用 shutil.copy2 逐个文件走，顺带能报进度、能取消。"""
    total = _size_of(src)
    done, last = 0, 0.0
    started = time.time()
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.rglob("*")):
        job.check()
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        done += p.stat().st_size
        if total and (done - last >= _STEP or done >= total):
            last = done
            speed = done / 1048576 / max(0.5, time.time() - started)
            report("download", None,
                   f"⧉ 正在搬家 {done // 1048576} MB / {total // 1048576} MB"
                   f"（{done * 100 // max(1, total)}%）· {speed:.0f} MB/s")


def _verify(home: Path, params: dict, job: Job, report) -> str:
    """搬完自检：锚点在、能跑、引擎自己认。返回空串 = 通过"""
    if not (home / _ANCHOR).is_file():
        return "新位置的引擎运行环境不完整"
    finesub = engine.find_finesub(str(params.get("finesub_exe") or ""))
    if not finesub:
        return ""                       # 引擎没装/找不到：没法自检，按通过处理（调用方本就没装引擎）
    env = os.environ.copy()
    env["FINESUB_HOME"] = str(home)     # 让引擎去新位置找
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([finesub, "doctor"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=180,
                           **no_window_kwargs())
    except Exception as e:  # noqa: BLE001
        return f"自检没跑起来：{type(e).__name__}"
    out = (r.stdout or "") + (r.stderr or "")
    job.check()
    if "runtime" in out and "ready" not in out.split("runtime", 1)[1][:40]:
        return "引擎在新位置没认出运行环境"
    if "models" not in out:
        return "引擎在新位置没认出模型目录"
    return ""


def move_home(params: dict, new_dir: str, job: Job, report) -> dict:
    """把整个数据目录搬到 new_dir。返回 {ok, moved_to, reason}

    ok=True  → 已经搬完并自检通过（调用方可以改设置、提示用户）
    ok=False → **原处一个字没动**（新位置的残留已清），reason 是人话；
               调用方可以退回「在新位置重新准备（要重下几个 GB）」那条老路。
    """
    src = Path((params.get("finesub_home") or "").strip() or datadir.default_home())
    dst = Path(str(new_dir)).expanduser()
    if not src.exists():
        # 还没装引擎（没有数据可搬）→ 不算失败：界面直接把设置指过去就行
        return {"ok": True, "moved_to": "", "reason": "", "note": "还没有数据目录"}

    why = _check_target(src, dst)
    if why:
        return {"ok": False, "reason": why, "fallback": False}   # 目标不合法：别自作主张重下

    same = _same_volume(src, dst)
    size_gb = _size_of(src) / 1073741824
    report("engine", None,
           f"正在把引擎数据搬到 {dst}（{size_gb:.1f} GB，"
           + ("同一块盘，改个名就好）…" if same else "跨盘复制，几分钟）…"))

    if same:
        try:
            shutil.move(str(src), str(dst))          # 同盘 = 改名，瞬时
        except OSError as e:
            return {"ok": False, "reason": f"搬家失败：{type(e).__name__}", "fallback": True}
    else:
        try:
            _copy_tree(src, dst, job, report)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(dst, ignore_errors=True)   # 半份清掉，原处不动
            return {"ok": False, "reason": f"复制没完成：{type(e).__name__}", "fallback": True}

    # 自检：不过就把新目录清掉（同盘的情况要把名字改回去），原处恢复
    try:
        bad = _verify(dst, params, job, report)
    except Exception as e:  # noqa: BLE001
        bad = f"自检异常：{type(e).__name__}"
    if bad:
        try:
            if same:
                shutil.move(str(dst), str(src))
            else:
                shutil.rmtree(dst, ignore_errors=True)
        except OSError:
            pass
        return {"ok": False, "reason": bad, "fallback": True}

    if not same:
        report("engine", None, "搬好了、自检通过，正在删掉原来的那份…")
        shutil.rmtree(src, ignore_errors=True)
    report("engine", None, f"✅ 引擎数据已搬到 {dst}（自检通过）")
    return {"ok": True, "moved_to": str(dst), "reason": ""}
