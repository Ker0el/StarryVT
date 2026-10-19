#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键打包：后端 → 界面（dotnet publish）→ 组装 _stage/app → Inno 安装包

用法: python build.py [--pyinstaller]
产物: _stage/app/（整包）、installer/StarryVT-Setup.exe

后端默认走 **Nuitka**（Python 编成本机码，不随包发 .pyc）—— 见 build_nuitka.py，
产物在 `dist-nuitka/app.dist/`。
加 `--pyinstaller` 退回老路线：产物在 `dist/StarryVT/`，里面有个 PYZ 归档，
我们的 .pyc 全在里面，拿 pyinstxtractor 一解就能还原源码。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
_ISCC_CANDIDATES = [
    Path(r"D:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(r"D:\Program Files\Inno Setup 6\ISCC.exe"),
]
ISCC = Path(os.environ.get("ISCC") or "")
if not ISCC.is_file():
    ISCC = next((p for p in _ISCC_CANDIDATES if p.is_file()), _ISCC_CANDIDATES[0])
BIN_DIR = ROOT / ".tools" / "bin"
WHEELHOUSE = ROOT / "wheelhouse"      # 随包分发的 Python 包（只放在 GitHub 上的那几个）
UI_PROJ = ROOT / "winui" / "StarryVt" / "StarryVt.csproj"
STAGE = ROOT / "_stage" / "app"

# 后端走哪条线：默认 Nuitka（防反编译）；--pyinstaller 退回旧路线
USE_PYINSTALLER = "--pyinstaller" in sys.argv
NUITKA_DIST = ROOT / "dist-nuitka" / "app.dist"
PYI_DIST = ROOT / "dist" / "StarryVT"
BACKEND_DIST = PYI_DIST if USE_PYINSTALLER else NUITKA_DIST

# 随包分发的外部程序（ffmpeg 另外还需要同目录的 av*.dll 共享库）
BUNDLED = ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe")

# 明确排除的模块：
#  - PyQt/PySide：pywebview 的可选后端，本应用不用（且 PyQt 是 GPL/商业双许可，商用不能误打包）
#  - tkinter/matplotlib/科学计算栈：应用完全不依赖，打进去只会变胖
EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "PyQtWebEngine", "shiboken2", "shiboken6",
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "notebook", "jupyter",
    "pytest", "setuptools", "pip", "wheel", "cv2", "torch", "transformers", "pygame",
]


def main() -> int:
    print("=== [0/3] 检查随包二进制 ===")
    lack = [f for f in BUNDLED if not (BIN_DIR / f).exists()]
    if lack or not list(BIN_DIR.glob("av*.dll")):
        print(f"!! 缺少随包程序：{', '.join(lack) or 'ffmpeg 的共享库 av*.dll'}")
        print("   先运行  python fetch_tools.py  把它们下载到 .tools/bin/")
        return 1
    print(f"    OK: {len(list(BIN_DIR.iterdir()))} 个文件，"
          f"{sum(f.stat().st_size for f in BIN_DIR.iterdir()) / 1048576:.0f} MB")

    # 引擎装运行时有一个包只放在 GitHub 上（uv 按写死的地址直连，绕不开）。
    # 没有代理的用户全靠这一份离线装上，缺了就等于"装完用不了"。见 wheels.py
    wheels = sorted(WHEELHOUSE.glob("*.whl")) if WHEELHOUSE.is_dir() else []
    if not wheels:
        print("!! 缺少随包分发的 Python 包（wheelhouse\\ 是空的）")
        print("   先运行  python fetch_tools.py --proxy http://127.0.0.1:7897")
        return 1
    print(f"    OK: wheelhouse {len(wheels)} 个包，"
          f"{sum(f.stat().st_size for f in wheels) / 1048576:.0f} MB")

    if USE_PYINSTALLER:
        print("=== [1/4] PyInstaller 构建（--pyinstaller）===")
        # ⚠️⚠️ **别把 dist/ 整个 rmtree 掉** —— 用户的成品视频就在
        #    `dist\StarryVT\output\` 里，那是他跑出来的东西、**不在任何仓库里**，
        #    删了就真没了（`_rebuild_backend.py` 上已经因为同类问题丢过一次，
        #    2026-10-16 顺手把这儿也拆掉）。
        #    做法：先把 output/ 挪出去，构建完再挪回来。
        _out = PYI_DIST / "output"
        _keep = ROOT / "_stage" / "_output_keep"
        if _out.exists():
            shutil.rmtree(_keep, ignore_errors=True)
            shutil.move(str(_out), str(_keep))
        try:
            shutil.rmtree(ROOT / "build", ignore_errors=True)
            shutil.rmtree(ROOT / "dist", ignore_errors=True)
        finally:
            if _keep.exists():
                _out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(_keep), str(_out))
                print("    （用户成品 output/ 已原样放回）")
        cmd = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", "StarryVT", "--icon", "assets/app.ico",
            "--add-data", "web;web", "--add-data", "assets;assets", "--add-data", "icon.png;.",
            "--add-data", "TERMS.md;.",     # 许可证清单构建后才生成，由安装包直接放安装根目录
            "--add-data", ".tools/bin;bin",  # ffmpeg/ffprobe/yt-dlp：随包分发，用户不必自己装
            "--add-data", "wheelhouse;wheelhouse",  # 只放在 GitHub 上的 Python 包（无代理用户离线装运行时用）
            "--add-data", "licenses;licenses", # 上面那些程序的许可证原文（LGPL/GPL/Unlicense）
            "--collect-submodules", "mcp",     # MCP 服务（app.py --mcp / --mcp-http）
            "--hidden-import", "mcp_server",
            *[f"--exclude-module={m}" for m in EXCLUDES],
            "app.py",
        ]
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print("!! PyInstaller 构建失败")
            return r.returncode
    else:
        print("=== [1/4] Nuitka 构建（几分钟；第一次会下载 depends.exe）===")
        # 参数、data 映射、自检都在 build_nuitka.py 里，这里不重复一份
        r = subprocess.run([sys.executable, "build_nuitka.py"], cwd=ROOT)
        if r.returncode != 0:
            print("!! Nuitka 构建失败")
            return r.returncode

    if not (BACKEND_DIST / "StarryVT.exe").exists():
        print(f"!! 后端产物里没有 StarryVT.exe: {BACKEND_DIST}")
        return 1

    print("=== [2/4] 生成第三方组件许可证清单 ===")
    subprocess.run([sys.executable, "make_licenses.py"], cwd=ROOT)

    print("=== [3/4] 生成说明页 rtf（改过 installer/说明.txt 才会变） ===")
    r = subprocess.run([sys.executable, "mkrtf.py"], cwd=ROOT / "installer")
    if r.returncode != 0:
        print("!! 说明页生成失败（链接点不动 / 有非 ASCII 残留）")
        return r.returncode

    print("=== [4/4] 组装 _stage\\app（界面 + 后端 + 文档） ===")
    if not UI_PROJ.exists():
        print(f"!! 找不到界面工程: {UI_PROJ}")
        return 1
    shutil.rmtree(STAGE, ignore_errors=True)
    # -o 用正斜杠：反斜杠在 bash 里会被 MSBuild 拼错（实测会生成一个 _stageapp 之类的怪目录）
    r = subprocess.run(["dotnet", "publish", str(UI_PROJ), "-c", "Release",
                        "-p:Platform=x64", "-p:RuntimeIdentifier=win-x64",
                        "-o", str(STAGE).replace("\\", "/"), "--nologo"], cwd=ROOT)
    if r.returncode != 0:
        print("!! 界面 publish 失败")
        return r.returncode
    # publish 不带 xbf / .pri（csproj 里有补救的 target），这里验一下，缺了就别往下走
    lack = [n for n in ("App.xbf", "MainWindow.xbf", "StarryVt.pri", "Pages/OnlinePage.xbf")
            if not (STAGE / n).exists()]
    if lack:
        print(f"!! publish 产物缺少编译后的 XAML/资源索引: {', '.join(lack)}")
        print("   装上去会一启动就 XAML parsing failed，先修 csproj 的 CopyXamlArtifactsToPublish")
        return 1

    # 后端整棵放进 backend\ —— 界面按 AppContext.BaseDirectory\backend\StarryVT.exe 找它
    # （两种布局都行：界面找 exe 只看这一个路径，工具目录的差异在 Ffmpeg.cs 里兼容掉了）
    shutil.rmtree(STAGE / "backend", ignore_errors=True)
    shutil.copytree(BACKEND_DIST, STAGE / "backend")
    for doc in ("TERMS.md", "THIRD_PARTY_LICENSES.md", "LICENSE", "COPYRIGHT.md"):
        if (ROOT / doc).exists():
            shutil.copy2(ROOT / doc, STAGE / doc)
    if (ROOT / "licenses").is_dir():
        shutil.rmtree(STAGE / "licenses", ignore_errors=True)
        shutil.copytree(ROOT / "licenses", STAGE / "licenses")
    size = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file()) / 1048576
    print(f"    OK: {STAGE} （{size:.0f} MB）")

    if not ISCC.exists():
        print(f"!! 找不到 Inno Setup 编译器: {ISCC}（跳过安装包生成）")
        return 0
    print("=== 生成安装程序 ===")
    r = subprocess.run([str(ISCC), "installer.iss"], cwd=ROOT)
    if r.returncode != 0:
        print("!! 安装程序编译失败")
        return r.returncode
    print("\n[OK] 完成：")
    print(f"   后端:   {BACKEND_DIST}")
    print(f"   整包:   {STAGE}")
    print(f"   安装包: {ROOT / 'installer' / 'StarryVT-Setup.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
