#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安装包验收：沙箱静默装 → 查文件 → 起界面 → 静默卸 → 查残留 + 真机哨兵

用法:  python installer/acceptance_test.py

为什么要"真机哨兵"（这份脚本的重点）：
  · Inno 按 **AppId** 认卸载注册项，不看路径。沙箱里装同一个 AppId 的包，
    会把真机那条 `{AppId}_is1` **覆盖**成指向沙箱；接着沙箱卸载器按 AppId 删键，
    真机「设置 - 应用」里的条目就没了 —— 而**文件一个都没动**，光比目录是看不出来的。
    （2026-10-08 RizomUV 上实测踩过。）
  · 所以跑前跑后要比：真机安装目录指纹 + HKCU 卸载项**子键名集合**。
    注册表那条键整份备份、跑完写回，外面再套一层 try/finally。

⚠ 沙箱里**永远不要**跑真机自己的卸载器（会读注册表里的 App Path 去删真机目录）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent


def _find_setup() -> Path:
    """当前版本的安装包（文件名带版本号；老构建没带版本号时退回旧名）"""
    ver = ""
    try:
        for line in (ROOT / "installer.iss").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#define AppVersion"):
                ver = line.split('"')[1]
                break
    except (OSError, IndexError):
        pass
    if ver:
        p = ROOT / "installer" / f"StarryVT-Setup_v{ver}.exe"
        if p.is_file():
            return p
    return ROOT / "installer" / "StarryVT-Setup.exe"


SETUP = _find_setup()
SANDBOX = ROOT / "_stage" / "testinstall"
REAL_DIR = Path(r"D:\Other\StarryVT")          # 真机装的那一份（只读它，绝不碰）
APP_ID = "{7E3B9C2A-5D41-4A6E-9F2B-1C8E4A6D2B91}_is1"
UNINST_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
KEY = UNINST_ROOT + "\\" + APP_ID

# --upgrade：先按 2.x 摆一份老布局，测老用户升级这条路径
UPGRADE = "--upgrade" in sys.argv

fail: list[str] = []


def check(ok: bool, what: str) -> bool:
    print(("  [OK]   " if ok else "  [FAIL] ") + what)
    if not ok:
        fail.append(what)
    return ok


# ---------------------------------------------------------------- 哨兵

def fingerprint(d: Path) -> tuple[int, int, int]:
    """目录指纹：文件数 + 总字节 + 最新修改时间（比 md5 便宜，够看出增删改）"""
    cnt = tot = newest = 0
    if not d.exists():
        return (0, 0, 0)
    for p in d.rglob("*"):
        try:
            if p.is_file():
                st = p.stat()
                cnt += 1
                tot += st.st_size
                newest = max(newest, int(st.st_mtime))
        except OSError:
            pass
    return (cnt, tot, newest)


def reg_snapshot() -> dict:
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            i = 0
            while True:
                try:
                    name, val, kind = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[name] = (kind, val)
                i += 1
    except FileNotFoundError:
        pass
    return out


def reg_restore(snap: dict) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_ALL_ACCESS) as k:
        # 先删掉现在有的、快照里没有的
        i = 0
        while True:
            try:
                name = winreg.EnumValue(k, i)[0]
            except OSError:
                break
            if name not in snap:
                winreg.DeleteValue(k, name)
            else:
                i += 1
        for name, (kind, val) in snap.items():
            winreg.SetValueEx(k, name, 0, kind, val)


def uninst_keys() -> set[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_ROOT) as k:
            return {winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])}
    except OSError:
        return set()


# ---------------------------------------------------------------- 步骤

def seed_old_layout() -> None:
    """按 2.x 的样子先在沙箱里摆一份，用来测「老用户升级」这条路径

    ⚠️ 只摆哑文件：**绝不能**把真机那个卸载器拷进来跑（它读注册表里的 App Path 干活，
       会把真机目录当真删掉 —— IlluGen 上实测踩过，回收站都没有）。
    """
    print("  （升级模式：先在沙箱里摆一份 2.x 的老布局）")
    (SANDBOX / "_internal").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "web").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "output" / "某视频-20261013").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "StarryVT.exe").write_bytes(b"OLD-2X-APP" * 500)
    (SANDBOX / "_internal" / "python310.dll").write_bytes(b"x" * 4096)
    (SANDBOX / "web" / "index.html").write_text("<html>old ui</html>", encoding="utf-8")
    # 用户自己的东西：成品和随手放在安装目录里的文件，升级不该动它们
    (SANDBOX / "output" / "某视频-20261013" / "成片.mp4").write_bytes(b"user output")
    (SANDBOX / "我自己放的文件.txt").write_text("别删我", encoding="utf-8")


def install() -> bool:
    print("\n[1/4] 沙箱静默安装")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    if UPGRADE:
        seed_old_layout()
    log = ROOT / "_stage" / "install.log"
    r = subprocess.run([str(SETUP), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                        f"/DIR={SANDBOX}", f"/LOG={log}"], timeout=1800)
    print(f"  退出码 {r.returncode}，日志 {log}")
    if r.returncode != 0:
        check(False, "安装退出码为 0")
        txt = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else ""
        print("\n".join(txt.splitlines()[-25:]))
        return False
    txt = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else ""
    # ⚠️ 别用「行里有没有 error」筛 —— 文件名里就带 error（api-ms-win-core-errorhandling-*.dll），
    #    会把正常日志判成失败（第一次跑就这么误报的）
    bad = [ln for ln in txt.splitlines()
           if ("Error" in ln or "错误" in ln or "Failed" in ln)
           and "Dest filename" not in ln and "Source filename" not in ln]
    for ln in bad[:10]:
        print("   日志:", ln.strip())
    ok = check(not bad, "安装日志没有 Error")
    ok &= check("Installation process succeeded" in txt, "日志里有「安装成功」")
    return ok


def verify_files() -> bool:
    print("\n[2/4] 查装出来的东西")
    ok = True
    for rel in ("StarryVt.exe", "backend/StarryVT.exe", "backend/_internal/bin/yt-dlp.exe",
                "backend/_internal/bin/ffmpeg.exe", "App.xbf", "MainWindow.xbf", "StarryVt.pri",
                "Pages/OnlinePage.xbf", "Controls/SmoothTextBox.xbf",
                "TERMS.md", "THIRD_PARTY_LICENSES.md", "licenses"):
        ok &= check((SANDBOX / rel).exists(), f"{rel}")
    # 旧版（pywebview）的东西不该在
    for rel in ("web", "_internal"):
        ok &= check(not (SANDBOX / rel).exists(), f"根目录没有旧的 {rel}")
    if UPGRADE:
        # 用户的成品和随手放的文件必须留着（升级只该清旧程序自己的那几个）
        ok &= check((SANDBOX / "output" / "某视频-20261013" / "成片.mp4").exists(),
                    "升级后用户的 output\\ 还在")
        ok &= check((SANDBOX / "我自己放的文件.txt").exists(),
                    "升级后用户自己放的文件还在")
    ok &= check((SANDBOX / "unins000.exe").exists(), "卸载器在")
    return ok


def run_app() -> bool:
    """真起一次界面：确认后端找得到（backend\\），XAML 都编进去了（能开页面、能截图）"""
    print("\n[3/4] 起一次装好的程序")
    import json
    exe = SANDBOX / "StarryVt.exe"
    # ⚠️ 用主页启动：主页一进来就会问后端要自检报告（Backend.Start 在里面），
    #    在线页不贴链接则一个后端请求都不发 —— 那样测不出"随包后端能不能找到"
    p = subprocess.Popen([str(exe), "--page=home"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    time.sleep(20)

    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'StarryVT.exe' } | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        procs = json.loads(out.stdout.strip() or "[]")
        procs = procs if isinstance(procs, list) else [procs]
    except Exception:
        procs = []
    backend = [x for x in procs if "backend" in (x.get("CommandLine") or "")
               and str(SANDBOX) in (x.get("CommandLine") or "")]
    print(f"  界面进程 pid={p.pid}，随包后端 {len(backend)} 个")
    for x in backend:
        print("   ", (x.get("CommandLine") or "")[:130])

    # 截图：页面 xbf 缺失的话这时候窗口是空的/直接异常退出，一眼就能看出来
    shot = ROOT / "_stage" / "installed_shot.png"
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / ".tools" / "capture_window.ps1"), "-ProcName", "StarryVt",
                    "-Out", str(shot)], capture_output=True, text=True)
    size = shot.stat().st_size if shot.exists() else 0
    print(f"  截图 {shot} ({size / 1024:.0f} KB)")

    ok = check(p.poll() is None, "界面起来了没崩")
    ok &= check(bool(backend), "用的是随包后端（路径在沙箱的 backend\\ 下）")
    ok &= check(size > 50_000, "窗口截得出来（XAML 都在）")

    dump = Path(os.environ.get("TEMP", "")) / "starryvt-winui-crash.txt"
    (ROOT / "_stage" / "run.log").write_text(
        f"pid={p.pid}\n{json.dumps(procs, ensure_ascii=False, indent=1)}\n"
        f"dump={dump.read_text(encoding='utf-8', errors='replace') if dump.exists() else '(无)'}\n",
        encoding="utf-8")

    # 收工：界面 + 它拉起的后端一起关掉（按 pid 杀，别按名字 —— 本机还跑着开发版）
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True)
    time.sleep(2)
    return ok


def uninstall() -> bool:
    print("\n[4/4] 静默卸载 + 查残留")
    log = ROOT / "_stage" / "uninstall.log"
    r = subprocess.run([str(SANDBOX / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                        "/NORESTART", f"/LOG={log}"], timeout=1800)
    print(f"  退出码 {r.returncode}")
    time.sleep(3)
    left = sorted(p.name for p in SANDBOX.iterdir()) if SANDBOX.exists() else []
    print(f"  沙箱里剩下的：{left or '（空）'}")
    ok = check(r.returncode == 0, "卸载退出码为 0")
    ok &= check(not left, "安装目录清干净了")
    return ok


def main() -> int:
    print("=" * 70)
    print("星译 3.0 安装包验收")
    print(f"  安装包: {SETUP}  ({SETUP.stat().st_size / 1048576:.0f} MB)" if SETUP.exists()
          else f"  安装包不存在: {SETUP}")
    if not SETUP.exists():
        return 1

    snap = reg_snapshot()
    keys_before = uninst_keys()
    fp_before = fingerprint(REAL_DIR)
    print(f"  真机哨兵: 目录 {fp_before} / 卸载项 {len(keys_before)} 条 / 本 App 键 {len(snap)} 个值")

    code = 1
    try:
        # 每一步都跑完再说 —— 前面一项挂了也把后面的查完，一次就能看出全貌
        installed = install()
        if installed or (SANDBOX / "StarryVt.exe").exists():
            if verify_files():
                run_app()
        if (SANDBOX / "unins000.exe").exists():
            uninstall()
        code = 0
    finally:
        # 不管中间出什么事，注册表都要还原（沙箱装/卸动的是同一个 AppId 的那条键）
        reg_restore(snap)
        keys_after = uninst_keys()
        fp_after = fingerprint(REAL_DIR)
        print("\n[哨兵] 真机核对")
        check(fp_after == fp_before, f"真机安装目录没被动过 {fp_before} -> {fp_after}")
        check(keys_after == keys_before,
              f"卸载注册项集合一致（多了 {sorted(keys_after - keys_before)}，"
              f"少了 {sorted(keys_before - keys_after)}）")
        check(reg_snapshot() == snap, "本 App 的卸载键已还原")

    print("\n" + "=" * 70)
    if fail:
        print(f"验收失败 {len(fail)} 项：")
        for f in fail:
            print("  ·", f)
        return 1
    print("验收全部通过 ✅")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
