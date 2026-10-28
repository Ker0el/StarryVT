#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""星译自己的数据目录：语音克隆、配音缓存、缩略图、wheel 缓存都放这里。

**为什么要有**（2026-10-21 用户报障）：
  语音克隆的目录以前是写死在 `%LOCALAPPDATA%\\StarryVT\\voiceclone` 的，
  而设置里只有「引擎数据目录」那一项，用户找不到换盘的地方，15 GB 只能堆在 C 盘。

**现在的规则**：
  - 默认根目录 `%LOCALAPPDATA%\\StarryVT`（和语音克隆原来的位置一致，老用户不搬东西）
  - 设置里的 `data_home` 非空时，整个根目录换成那个（设置 → 输出 → 数据目录）
  - 老版本的 `dub_cache` / `wheels` / `subtitle_preview` 在 `%APPDATA%\\StarryVT` 下，
    第一次用到时自动搬进根目录（同盘只是改名，瞬时）

**不跟着走的东西**（都很小，或者必须在固定位置）：
  - `%APPDATA%\\StarryVT`：settings.json、日志、bin（uv/yt-dlp，约 60 MB）
  - `%LOCALAPPDATA%\\StarryVT-WinUI`：壁纸和界面状态（约 50 MB）
  - 翻译引擎：由设置里的「引擎数据目录」单独管（见 datadir.py）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

#: 参与统一管理的子目录。顺序只影响搬家的先后（大的在前，进度更有意义）
ITEMS = ("voiceclone", "dub_cache", "thumbs", "wheels", "subtitle_preview")

#: 复制时每这么多字节报一次进度
_STEP = 256 << 20

_state: dict = {"mtime": None, "value": ""}


def config_dir() -> Path:
    """配置和日志目录（不跟着 data_home 走）"""
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT"


def legacy_root() -> Path:
    """老版本的数据根目录（6.12 及以前）"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StarryVT"


def app_dir() -> Path:
    """程序根目录（打包后是 backend 的上一层；开发时是源码目录）

    打包态要先过 long_path：用 8.3 短路径起的进程，`sys.executable` 返回的就是
    短路径，`<app>\\data\\engine` 会跟着变成 `D:\\PROGRA~1\\…` 并被写进设置。
    """
    import tools
    if tools.is_packaged():
        # {app}\backend\StarryVT.exe -> {app}
        return tools.exec_path().parent.parent
    return Path(__file__).parent


def app_data_root() -> Path:
    """跟软件放在一起的数据目录：{app}\\data"""
    return app_dir() / "data"


_default_cache: Path | None = None


def reset_default() -> None:
    """设置变了（换数据目录 / 搬完家）之后重新算默认位置"""
    global _default_cache
    _default_cache = None


def _in_temp(p: Path) -> bool:
    """程序是不是装在 %TEMP% 里（自动更新装错位置时的兜底，别把数据放那儿）"""
    try:
        t = Path(tempfile.gettempdir()).resolve()
        r = p.resolve()
        return r == t or t in r.parents
    except OSError:
        return True


def _usable_root(p: Path) -> bool:
    """目录能不能写（真建一个探针文件，别只看权限位）"""
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".starryvt-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _has_legacy_data(p: Path) -> bool:
    """这台机器上是不是已经有老版本的数据。

    ⚠️ 不看 `thumbs`：它太小、创建得太早，不能拿它当「老用户」的判据。
    """
    return any((p / n).is_dir()
               for n in ("voiceclone", "dub_cache", "wheels", "subtitle_preview"))


def default_root() -> Path:
    """默认数据根目录（每个进程只决定一次）。

    · 新用户：**跟软件放在一起** `<app>\\data`（用户 2026-10-21 定）
    · 老用户：继续用 `%LOCALAPPDATA%\\StarryVT`（数据已经在那儿，绝不偷偷搬）
    · 软件装在临时目录 / 目录写不进去：退回 `%LOCALAPPDATA%\\StarryVT`
    """
    global _default_cache
    if _default_cache is not None:
        return _default_cache
    legacy = _long(legacy_root())
    if _has_legacy_data(legacy):
        _default_cache = legacy
        return legacy
    adj = _long(app_data_root())
    if not _in_temp(adj) and _usable_root(adj):
        _default_cache = adj
        return adj
    _default_cache = _long(legacy)
    return _default_cache


def custom_root() -> str:
    """设置里显式指定的数据根目录（空串 = 用默认）"""
    return _custom()


def is_app_data_root() -> bool:
    """当前根目录是不是「跟软件放在一起」那个"""
    try:
        return root().resolve() == app_data_root().resolve()
    except OSError:
        return False


def _custom() -> str:
    """设置里的 data_home。带 mtime 缓存：缩略图那种高频调用不会反复读 JSON。"""
    try:
        import settings as cfg
        try:
            mtime = cfg.SETTINGS_PATH.stat().st_mtime_ns
        except OSError:
            _state["mtime"], _state["value"] = None, ""
            return ""
        if _state["mtime"] != mtime:
            _state["mtime"] = mtime
            _state["value"] = (cfg.load().get("data_home") or "").strip()
        return _state["value"]
    except Exception:  # noqa: BLE001 - 读不到设置就当没配，用默认位置
        return ""


def root() -> Path:
    """当前生效的数据根目录。

    配的盘不在了（移动硬盘拔掉之类）就退回默认位置，别让缩略图/配音整个挂掉。
    """
    raw = _custom()
    if not raw:
        return default_root()
    p = _long(Path(raw).expanduser())
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return default_root()


def _long(p: Path) -> Path:
    r"""换成真实长路径（`D:\PROGRA~1` → `D:\Program Files`）。

    2026-10-28 用户在设置页看到「位置：D:\PROGRA~1\…\data\voiceclone」——
    数据根目录是跟着程序目录算的，进程若用短路径启动，这条链会一路带短名。
    tools.long_path 是幂等的：本来就是长路径时原样返回。
    """
    try:
        import tools
        return tools.long_path(p)
    except Exception:  # noqa: BLE001 - 取不到就原样用
        return p


def sub(name: str) -> Path:
    """根目录下的某个子目录（**不**做迁移，纯算路径）"""
    return root() / name


def legacy(name: str) -> Path:
    """老版本放这个子目录的地方"""
    return config_dir() / name


def resolve(name: str) -> Path:
    """优先新根目录；新位置没有、旧位置有 → 先把旧目录搬过来再用。

    搬不动就继续用旧位置 —— 宁可目录不统一，也不能把功能弄坏。
    只在「第一次用到」时发生一次（同盘是改名，跨盘是复制）。
    """
    new = sub(name)
    old = legacy(name)
    if new.exists() or not old.exists():
        return new
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        return new
    except OSError:
        return old


def migrate_legacy() -> list[str]:
    """把老配置目录里散着的缓存并进当前根目录（启动时调一次）。

    返回搬了哪些（给日志看）。同盘是改名，瞬时；搬不动就留在原处继续用。
    """
    moved: list[str] = []
    for name in ("dub_cache", "wheels", "subtitle_preview"):
        old = legacy(name)
        if not old.is_dir():
            continue
        got = resolve(name)
        if (os.path.normcase(str(got)) != os.path.normcase(str(old))
                and not old.exists()):
            moved.append(f"{old} → {got}")
    return moved


# ---------- 搬家 ----------

def _size_of(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return (os.path.splitdrive(str(a.resolve()))[0].lower()
                == os.path.splitdrive(str(b.resolve()))[0].lower())
    except OSError:
        return False


def _copy_tree(src: Path, dst: Path, job=None, report=None, total: int | None = None) -> None:
    """跨盘复制（有进度、可取消）。复制成功后才由调用方删源。"""
    if total is None:
        total = _size_of(src)
    done, last = 0, 0.0
    started = time.time()
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.rglob("*")):
        if job is not None:
            job.check()
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        try:
            done += p.stat().st_size
        except OSError:
            pass
        if report and total and (done - last >= _STEP or done >= total):
            last = done
            speed = done / 1048576 / max(0.5, time.time() - started)
            report("data", None,
                   f"⧉ 正在搬数据 {done // 1048576} MB / {total // 1048576} MB"
                   f"（{done * 100 // max(1, total)}%）· {speed:.0f} MB/s")


def _move_one(src: Path, dst: Path, job=None, report=None,
              total: int | None = None) -> None:
    """搬一个子目录：同盘改名（瞬时），跨盘复制完再删源。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _same_volume(src, dst):
        shutil.move(str(src), str(dst))
    else:
        _copy_tree(src, dst, job, report, total=total)
        shutil.rmtree(src, ignore_errors=True)


def _check_target(old: Path, new: Path, need: int) -> str:
    """返回一句人话的拒绝理由；可以搬就返回空串"""
    try:
        o, n = old.resolve(), new.resolve()
    except OSError as e:
        return f"路径解析不了：{e}"
    if o == n:
        return "新位置就是现在的位置，不用搬"
    if o in n.parents:
        return "新位置不能放在数据目录里面"
    if n in o.parents:
        return "数据目录不能放进新位置里面"
    if n.exists() and not n.is_dir():
        return "新位置不是文件夹"
    if n.parent == n:
        return "别直接选整个盘，选一个文件夹（比如 D:\\StarryVT）"
    for name in ITEMS:
        d = n / name
        try:
            if d.exists() and any(d.iterdir()):
                return f"新位置里已经有 {name}，而且不是空的：{d}"
        except OSError as e:
            return f"读不了新位置：{e}"
    try:
        n.mkdir(parents=True, exist_ok=True)
        probe = n / ".starryvt-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return f"新位置写不进去：{type(e).__name__}"
    try:
        free = shutil.disk_usage(n).free
    except OSError:
        return ""
    if free < need + (512 << 20):
        return (f"那个盘空间不够：要 {need / 1073741824:.1f} GB，"
                f"只剩 {free / 1073741824:.1f} GB")
    return ""


def _save(new: Path) -> str:
    import settings as cfg
    try:
        same = new.resolve() == default_root().resolve()
    except OSError:
        same = False
    value = "" if same else str(new)
    cfg.save({"data_home": value})
    reset_default()          # 数据已经搬走了，默认位置重新算
    return value


def move_root(new_dir: str, job=None, report=None) -> dict:
    """把整个数据根目录搬到 new_dir。

    返回 {ok, moved_to, moved, reason}：
      ok=True  → 数据已经搬完（或者本来就没有数据），设置也改好了
      ok=False → **原处一个字没动**（新位置的残留已尽量清掉），reason 是人话
    """
    # 先把老配置目录里散着的那几个并进当前根目录，免得搬家漏下它们
    for name in ITEMS:
        resolve(name)

    old = root()
    new = Path(str(new_dir)).expanduser()
    try:
        if old.resolve() == new.resolve():
            return {"ok": True, "moved_to": str(new), "saved": _custom(),
                    "moved": [], "reason": ""}     # 选的就是现在的位置，什么都不用做
    except OSError:
        pass
    sizes = {name: _size_of(old / name) for name in ITEMS if (old / name).is_dir()}
    why = _check_target(old, new, sum(sizes.values()))
    if why:
        return {"ok": False, "moved_to": "", "moved": [], "reason": why}

    pairs = [(old / name, new / name) for name in ITEMS if name in sizes]
    if not pairs:
        saved = _save(new)
        return {"ok": True, "moved_to": str(new), "saved": saved,
                "moved": [], "reason": ""}

    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in pairs:
            size = sizes.get(src.name, 0)
            if report:
                report("data", None,
                       f"正在搬 {src.name}（{size / 1073741824:.1f} GB）…")
            _move_one(src, dst, job, report, total=size)
            done.append((src, dst))
    except Exception as e:  # noqa: BLE001 - 任何失败都要尽量回滚
        for src, dst in reversed(done):
            try:
                if dst.exists() and not src.exists():
                    _move_one(dst, src)
            except OSError:
                pass
        try:
            new.rmdir()          # 只在空的时候成功
        except OSError:
            pass
        return {"ok": False, "moved_to": "", "moved": [],
                "reason": f"搬家失败：{type(e).__name__}: {e}"}

    saved = _save(new)

    # 清掉旧根目录里的空壳（用户放在里面的别的文件绝不动）
    for name in ITEMS:
        try:
            (old / name).rmdir()
        except OSError:
            pass
    try:
        if old != default_root():
            old.rmdir()
    except OSError:
        pass
    return {"ok": True, "moved_to": str(new), "saved": saved,
            "moved": [src.name for src, _ in done], "reason": ""}


def info(with_sizes: bool = False) -> dict:
    """给界面 / 诊断报告用：实际位置和各项体积。

    ⚠️ 默认**不算体积** —— 语音克隆那 15 GB 走一遍要好几秒，
       而 `diag.start_session()` 每次启动都会调这个。只有导出诊断报告
       （deep=True）才真去数。
    """
    r = root()
    items = {}
    for name in ITEMS:
        p = r / name
        if not p.is_dir():
            items[name] = 0
        elif with_sizes:
            try:
                items[name] = round(_size_of(p) / 1073741824, 2)
            except OSError:
                items[name] = 0
        else:
            items[name] = None
    return {"root": str(r), "custom": _custom(), "default": str(default_root()),
            "legacy": {n: str(legacy(n)) for n in ITEMS}, "items_gb": items}
