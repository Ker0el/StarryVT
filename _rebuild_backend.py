#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只重建开发版后端（dist/StarryVT），不碰界面、不碰安装包。

⚠️ 故意不调 build.py --pyinstaller —— 那里面有 shutil.rmtree(ROOT/'dist')，
   会把用户跑出来的 dist/StarryVT/output/ 一起删掉。这里先把它挪出来再重建。
"""
import shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
BIN_DIR = ROOT / ".tools" / "bin"
WHEELHOUSE = ROOT / "wheelhouse"
ENGINE_WHL = ROOT / "engine_whl"
DST = ROOT / "dist" / "StarryVT"
TMP = ROOT / "_stage" / "_rebuild"
KEEP = ROOT / "_stage" / "_output_keep"

EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "PyQtWebEngine", "shiboken2", "shiboken6",
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "notebook", "jupyter",
    "pytest", "setuptools", "pip", "wheel", "cv2", "torch", "transformers", "pygame",
]

# 前置检查：--add-data 的源目录缺一个，PyInstaller 会直接报错
need = [ROOT / "web", ROOT / "assets", ROOT / "icon.png", ROOT / "TERMS.md",
        ROOT / "licenses", BIN_DIR, WHEELHOUSE, ENGINE_WHL, ROOT / "builtin_knowledge",
        ROOT / "clone_synth.py", ROOT / "clone_download.py"]   # 少了这个用户那边点克隆就报错
miss = [str(p) for p in need if not p.exists()]
if miss:
    print("!! 缺目录/文件，先别编："); [print("   ", m) for m in miss]; sys.exit(1)
print(f"前置检查 OK：wheelhouse {len(list(WHEELHOUSE.glob('*.whl')))} 个包，"
      f"engine_whl {len(list(ENGINE_WHL.glob('*.whl')))} 个包，"
      f".tools/bin {len(list(BIN_DIR.iterdir()))} 个文件")

if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)

cmd = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", "StarryVT", "--icon", str(ROOT / "assets" / "app.ico"),
    "--distpath", str(TMP / "dist"), "--workpath", str(TMP / "build"),
    "--specpath", str(TMP),
    # --add-data 的源路径必须写绝对路径：给了 --specpath 之后，相对路径是按
    # **spec 目录**解析的、不是按 cwd —— 否则报 "Unable to find ..._rebuild\web"
    "--add-data", f"{ROOT / 'web'};web", "--add-data", f"{ROOT / 'assets'};assets",
    "--add-data", f"{ROOT / 'icon.png'};.",
    "--add-data", f"{ROOT / 'TERMS.md'};.",
    # 第三方清单也要带上：设置页那个「感谢 → 完整许可证」按钮就靠它
    "--add-data", f"{ROOT / 'THIRD_PARTY_LICENSES.md'};.",
    # ★ 本地语音克隆的辅助脚本：拿克隆环境自己的 python（3.11）跑，必须随包。
    #   ⚠️ 必须放**独立子目录 clone/**：脚本的 sys.path[0] 是它所在目录，而这里
    #   躺着 python312.dll / _ctypes.pyd，3.11 解释器一 import 就撞车。
    "--add-data", f"{ROOT / 'clone_synth.py'};clone",
    "--add-data", f"{ROOT / 'clone_download.py'};clone",
    "--add-data", f"{BIN_DIR};bin",
    "--add-data", f"{WHEELHOUSE};wheelhouse",
    "--add-data", f"{ENGINE_WHL};engine_whl",
    "--add-data", f"{ROOT / 'licenses'};licenses",
    # 内置术语词条（builtin_kb.py 装引擎/启动时导进用户的词条库）
    "--add-data", f"{ROOT / 'builtin_knowledge'};builtin_knowledge",
    # MCP 服务（app.py --mcp / --mcp-http）
    "--collect-submodules", "mcp",
    "--hidden-import", "mcp_server",
    # 统一数据目录（语音克隆 / 缓存跟着 data_home 走）
    "--hidden-import", "paths",
    *[f"--exclude-module={m}" for m in EXCLUDES],
    str(ROOT / "app.py"),
]
t0 = time.time()
print("=== PyInstaller 构建中（几分钟）===", flush=True)
r = subprocess.run(cmd, cwd=ROOT)
if r.returncode != 0:
    print(f"!! 构建失败（{time.time()-t0:.0f}s）"); sys.exit(r.returncode)

new = TMP / "dist" / "StarryVT"
if not (new / "StarryVT.exe").exists():
    print("!! 构建产物里没有 StarryVT.exe"); sys.exit(1)
size = sum(f.stat().st_size for f in new.rglob("*") if f.is_file())
print(f"=== 构建完成 {time.time()-t0:.0f}s，{size/1048576:.0f} MB ===", flush=True)

# 换上去：先把 output 挪出来
if KEEP.exists():
    shutil.rmtree(KEEP, ignore_errors=True)
had_output = (DST / "output").exists()
if had_output:
    shutil.move(str(DST / "output"), str(KEEP))
    print(f"output/ 已挪走（{sum(1 for _ in KEEP.rglob('*'))} 项）")
try:
    shutil.rmtree(DST, ignore_errors=True)
    # ⚠️ rmtree(ignore_errors=True) 在 **exe 被占用** 时会静默失败、什么也不报；
    #    接着 copytree 撞上「目录已存在」抛一个 FileExistsError ——
    #    那个报错完全看不出「是因为开发版还开着」。这里先自己判一下，说人话。
    #    （2026-10-16 踩过：崩在中途，用户的 output/ 就滞留在 _output_keep 里没挪回来。）
    if DST.exists():
        raise SystemExit(
            f"\n[停] {DST} 没删干净 —— 多半是**开发版还开着**，exe 被占用。\n"
            f"     先关掉正在跑的那个星译，再重跑本脚本。\n"
            f"     （你的 output/ 现在安全地放在 {KEEP}，重跑会自动放回去。）")
    shutil.copytree(new, DST)
finally:
    # ★ 不管中间怎么炸，output/ 都必须挪回去 —— 那是用户跑出来的成品，不在任何仓库里
    if had_output and KEEP.exists() and not (DST / "output").exists():
        shutil.move(str(KEEP), str(DST / "output"))
        print("output/ 已放回")

print("\n[OK] 后端已更新:")
print("   ", DST / "StarryVT.exe")
print("   output/ 内容:", [p.name for p in (DST / "output").iterdir()] if (DST / "output").exists() else "(无)")
