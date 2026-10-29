#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nuitka 版后端打包 —— 和 build.py 里 PyInstaller 那一步等价，产物布局也一致。

为什么要换：
  PyInstaller 打出来的是 .pyc，反编译回源码只要几分钟；
  Nuitka 把 Python 编译成 C 再编成本机码，源码不再随包分发（用户 2026-10-14：
  「真要用 Nuitka 提高门槛」）。实测同一份源码：
      PyInstaller 383 MB   →   Nuitka 262 MB（少 121 MB，且更快）
      PyInstaller 布局 dist/StarryVT/{StarryVT.exe, _internal/...}
      Nuitka     布局 dist-nuitka/app.dist/{StarryVT.exe, bin/, web/, ...}

★★ 一个必须记住的坑：`--include-data-dir` 会**静默丢掉 .exe / .dll / .bin**。
   它们都在 Nuitka 的 default_ignored_suffixes 里（见 freezer/IncludedDataFiles.py），
   而 .tools/bin 里恰好全是 exe 和 dll —— 用它的话 ffmpeg/ffprobe/yt-dlp
   一个都不会进包，构建**照样报成功**，只在日志里留一行
   `WARNING: No data files in directory '.tools\\bin'.`，
   装到用户机器上才发现"下载器打不开"。
   → 随包二进制一律走 **--include-raw-dir**（raw=True，不走那套默认忽略）。

用法: python build_nuitka.py
产物: dist-nuitka/app.dist/（喂给 build.py 的话，整棵拷成 _stage/app/backend/）
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
BIN_DIR = ROOT / ".tools" / "bin"
WHEELHOUSE = ROOT / "wheelhouse"
ENGINE_WHL = ROOT / "engine_whl"      # 翻译引擎本体 + 它的 Python 依赖（离线装引擎用）
OUT = ROOT / "dist-nuitka"
DIST = OUT / "app.dist"

# 随包分发的外部程序（ffmpeg 另外还需要同目录的 av*.dll 共享库）
BUNDLED = ("ffmpeg.exe", "ffprobe.exe", "yt-dlp.exe")

# 和 build.py 保持一致：PyQt/PySide 是 pywebview 的可选后端（本应用不用，
# 且 PyQt 是 GPL/商业双许可）；tkinter / 科学计算栈应用完全不依赖。
EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "PyQtWebEngine", "shiboken2", "shiboken6",
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "IPython", "notebook", "jupyter",
    "pytest", "setuptools", "pip", "wheel", "cv2", "torch", "transformers", "pygame",
]


def main() -> int:
    print("=== [0/2] 检查随包二进制 ===")
    lack = [f for f in BUNDLED if not (BIN_DIR / f).exists()]
    if lack or not list(BIN_DIR.glob("av*.dll")):
        print(f"!! 缺少随包程序：{', '.join(lack) or 'ffmpeg 的共享库 av*.dll'}")
        print("   先运行  python fetch_tools.py  把它们下载到 .tools/bin/")
        return 1
    print(f"    OK: {len(list(BIN_DIR.iterdir()))} 个文件，"
          f"{sum(f.stat().st_size for f in BIN_DIR.iterdir()) / 1048576:.0f} MB")

    wheels = sorted(WHEELHOUSE.glob("*.whl")) if WHEELHOUSE.is_dir() else []
    if not wheels:
        print("!! 缺少随包分发的 Python 包（wheelhouse\\ 是空的）")
        print("   先运行  python fetch_tools.py --proxy http://127.0.0.1:7897")
        return 1
    print(f"    OK: wheelhouse {len(wheels)} 个包，"
          f"{sum(f.stat().st_size for f in wheels) / 1048576:.0f} MB")

    engine_wheels = sorted(ENGINE_WHL.glob("*.whl")) if ENGINE_WHL.is_dir() else []
    if not engine_wheels or not any(f.name.startswith("finesub-") for f in engine_wheels):
        print("!! 缺少随包分发的翻译引擎（engine_whl\\ 里没有 finesub-*.whl）")
        print("   先运行  python fetch_tools.py --proxy http://127.0.0.1:7897")
        return 1
    print(f"    OK: engine_whl {len(engine_wheels)} 个包，"
          f"{sum(f.stat().st_size for f in engine_wheels) / 1048576:.0f} MB")

    print("=== [1/2] Nuitka 构建（几分钟，第一次会下载 depends.exe）===")
    shutil.rmtree(OUT, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",       # 首次构建要下 depends.exe，别卡在交互确认上
        "--windows-console-mode=disable",
        # ⚠️ 全部用 `--选项=值` 的形式。`--output-dir dist-nuitka`（空格分开）会被
        #    optparse 拒掉：`error: The '--output-dir' option requires an argument with '--output-dir='`。
        "--output-dir=dist-nuitka",
        "--output-filename=StarryVT.exe",
        "--windows-icon-from-ico=assets/app.ico",
        "--include-data-dir=web=web",
        "--include-data-dir=assets=assets",
        "--include-data-file=icon.png=icon.png",
        "--include-data-file=TERMS.md=TERMS.md",
        # 第三方清单：设置页「感谢 → 完整许可证」用它（不带上那个按钮就是死的）
        "--include-data-file=THIRD_PARTY_LICENSES.md=THIRD_PARTY_LICENSES.md",
        # ★ 本地语音克隆的两个辅助脚本：它们是**拿克隆环境自己的 python（3.11）跑**的
        #   （localclone.py 用 `SCRIPT_DIR / "clone_synth.py"` 定位）。
        #   ⚠️⚠️ 必须放进**独立子目录 clone/**，不能跟 exe 摆同一层：脚本的
        #   `sys.path[0]` 是它自己所在目录，而 exe 旁边躺着 python312.dll / _ctypes.pyd
        #   —— 3.11 的解释器一 `import _ctypes` 就命中那个 3.12 的，当场报
        #   `Module use of python312.dll conflicts with this version of Python`。
        #   2026-10-15 实测：这两个脚本以前**两条打包路线都没带**，生产版上一点
        #   「本地语音克隆」必然失败（先是找不到文件，摆到根目录后就是上面那个 ImportError）。
        "--include-data-file=clone_synth.py=clone/clone_synth.py",
        "--include-data-file=clone_download.py=clone/clone_download.py",
        # ★ 必须放在 qwen/ 子目录：backend 根目录里有不完整的 backports.zstd，
        #   脚本放在根目录会被 sys.path 带进去，huggingface_hub 加载失败
        "--include-data-file=qwen/qwen_synth.py=qwen/qwen_synth.py",
        "--include-raw-dir=.tools/bin=bin",  # ★ 见文件头：这里不能用 include-data-dir
        "--include-data-dir=wheelhouse=wheelhouse",
        "--include-data-dir=engine_whl=engine_whl",  # 翻译引擎本体 + 依赖（离线安装）
        "--include-data-dir=licenses=licenses",
        # 内置术语词条（builtin_kb.py 导入用户的词条库用；装引擎那步和启动兜底都读它）
        "--include-data-dir=builtin_knowledge=builtin_knowledge",
        # MCP 服务（app.py --mcp / --mcp-http）。mcp 的 HTTP 模式会用到 uvicorn/starlette，
        # 只收 stdio 的客户端不需要，但打包时一起带上，避免两条路只有一条能用。
        "--include-package=mcp",
        # 快速模式的微软 TTS（LGPL-3.0）；它只在这里被懒加载，显式带上最保险
        "--include-package=edge_tts",
        "--include-module=mcp_server",
        # 统一数据目录（语音克隆 / 缓存都跟着 data_home 走）。显式列出，
        # 免得哪天改成只在函数里 import 被静态分析漏掉。
        "--include-module=paths",
        # ffmpeg 钉死（只在 install_finesub / _precheck 里延迟 import，显式带上）
        "--include-module=runtime_pin",
        # 微软运行库检测（同上，只在 engine._provision_runtime / pipeline._precheck /
        # diag 里延迟 import）
        "--include-module=vcruntime",
        *[f"--nofollow-import-to={m}" for m in EXCLUDES],
        "app.py",
    ]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0 or not (DIST / "StarryVT.exe").exists():
        print("!! Nuitka 构建失败（细节看上面输出 / nuitka-crash-report.xml）")
        return r.returncode or 1

    print("=== [2/2] 自检 ===")
    # ★ bin\ 必须真的落地 —— 这正是 include-data-dir 会静默漏掉的那批
    miss = [f for f in BUNDLED if not (DIST / "bin" / f).exists()]
    if miss:
        print(f"!! 包里缺随包程序：{', '.join(miss)}")
        print("   八成是又把 .tools/bin 写成 --include-data-dir 了，改回 --include-raw-dir")
        return 1
    for d in ("web", "assets", "licenses", "wheelhouse", "engine_whl", "builtin_knowledge"):
        if not (DIST / d).is_dir():
            print(f"!! 包里缺目录：{d}")
            return 1
    # ★ 克隆辅助脚本必须落在 exe 旁边的 clone/ 子目录里 —— 漏了不会报错，只会在用户点
    #   「本地语音克隆」时报一句「模型没加载起来」，完全指不到真正的原因
    for f in ("clone_synth.py", "clone_download.py"):
        if not (DIST / "clone" / f).is_file():
            print(f"!! 包里缺克隆辅助脚本：clone/{f}（用户点本地语音克隆会报「模型没加载起来」）")
            return 1
    # Qwen 的脚本放在 qwen/ 子目录（根目录的 backports.zstd 会污染 sys.path）
    for f in ("qwen_synth.py",):
        if not (DIST / "qwen" / f).is_file():
            print(f"!! 包里缺 Qwen 辅助脚本：qwen/{f}（Qwen3-TTS 会装不上 / 合不成）")
            return 1

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1048576
    print(f"\n[OK] 完成（{time.time() - t0:.0f} 秒）：")
    print(f"   后端:   {DIST}（{size:.0f} MB）")
    print(f"   装整包: 把 {DIST} 整棵拷成  _stage/app/backend/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
