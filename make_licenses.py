#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成第三方开源组件许可证清单（THIRD_PARTY_LICENSES.md）

扫打包产物里**实际带上**的包，从构建环境的包元数据里取许可证。
用法: python make_licenses.py

两种后端布局都要认（见 build.py / build_nuitka.py 里的选择）：
  · PyInstaller:  dist/StarryVT/_internal/     —— 纯 Python 模块压在 exe 里的 PYZ，这里只有二进制扩展
  · Nuitka:       dist-nuitka/app.dist/        —— 平铺布局（bin/ web/ assets/ 和一堆 .pyd）
                  dist-nuitka/app.build/       —— 每个被编译的模块都留一个 module.<点号名>.c

⚠️ Nuitka 那份**必须**连 app.build 一起扫：纯 Python 模块是被编译进 exe 的，
   产物目录里根本看不到它们 —— 只扫 app.dist 会漏掉一大半（实测 6 个 vs 14 个）。
   反过来，PyInstaller 那份只扫 _internal 也会漏纯 Python 模块（它们在 PYZ 里），
   不过那条线已经不用了，没再补。
"""
from __future__ import annotations

import importlib.metadata as md
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
PYI_INTERNAL = ROOT / "dist" / "StarryVT" / "_internal"
NUITKA_DIST = ROOT / "dist-nuitka" / "app.dist"
NUITKA_BUILD = ROOT / "dist-nuitka" / "app.build"
OUT = ROOT / "THIRD_PARTY_LICENSES.md"
COMBINED = ROOT / "licenses" / "PYTHON-PACKAGES-LICENSES.txt"
ENGINE_WHL = ROOT / "engine_whl"
ENGINE_LICENSES = ROOT / "licenses" / "ENGINE-WHEELS-LICENSES.txt"

# 打进包里但不在 site-packages 元数据里的（解释器本体等）
EXTRA = [
    ("Python (CPython)", "3.12", "PSF License (Python Software Foundation License)"),
    ("Microsoft Edge WebView2 Runtime", "系统组件", "Microsoft 软件许可条款（系统自带，非本软件分发）"),
    ("Microsoft Visual C++ Runtime", "系统组件", "Microsoft 可再发行组件许可"),
]

# 随包分发的外部程序（由 fetch_tools.py 下载到 .tools/bin/，版本运行时读取，不写死）
BUNDLED_BINARIES = [
    {
        "name": "FFmpeg（ffmpeg.exe / ffprobe.exe 及同目录 av*.dll）",
        "exe": "ffmpeg.exe",
        "arg": "-version",
        "license": "LGPL-3.0-or-later",
        "note": ("**随本软件分发**。刻意选用 LGPL 构建（不含 GPL 的 libx264/libx265），"
                 "以独立进程方式调用、未修改其代码，用户可自行替换。"
                 "许可证原文见 `licenses/LGPL-3.0.txt` 与 `licenses/GPL-3.0.txt`；"
                 "对应源码见 <https://ffmpeg.org/download.html>，"
                 "本构建的打包脚本见 <https://github.com/BtbN/FFmpeg-Builds>。"),
    },
    {
        "name": "yt-dlp（yt-dlp.exe）",
        "exe": "yt-dlp.exe",
        "arg": "--version",
        "license": "Unlicense（公有领域）",
        "note": "**随本软件分发**。许可证原文见 `licenses/yt-dlp-Unlicense.txt`；源码见 <https://github.com/yt-dlp/yt-dlp>。",
    },
]

# 随包分发的 Python 包（wheelhouse/）：引擎装运行时要用、而只发布在 GitHub 上的那几个。
# 没有代理的机器下不动它们，所以随安装包带一份原样副本（见 wheels.py）。
BUNDLED_WHEELS = [
    {
        "name": "CTranslate2（翻译引擎用的推理库）",
        "pattern": "ctranslate2-*.whl",
        "license": "MIT",
        "note": ("**随本软件分发**（位于程序目录的 `wheelhouse\\`）。上游为 OpenNMT 的 "
                 "CTranslate2（MIT）：<https://github.com/OpenNMT/CTranslate2>。"
                 "这里分发的是翻译引擎所需的 CUDA 构建副本，原样未改；"
                 "同时带 0.4.0 与 0.6.0 两个引擎版本对应的构建，运行时按引擎实际需要选用。"
                 "该包只在 GitHub 上发布，为保证没有代理的用户也能装好翻译引擎而随包提供。"
                 "该 wheel 还可能静态包含 ruy、FlatBuffers、SentencePiece、cpuinfo、CUTLASS 等"
                 "上游依赖（Apache-2.0 / BSD 等），各自许可证见对应项目仓库。"),
    },
]

# 随包分发的字体（位于 assets/fonts/）
BUNDLED_FONTS = [
    {
        "name": "思源黑体 Light（StarryHanSans-Light.ttf）",
        "license": "SIL Open Font License 1.1",
        "note": ("**随本软件分发**（位于程序目录的 `assets\\fonts\\`）。由 Noto Sans SC 可变字体抽取的 "
                 "Light（wght=300）静态实例；按 OFL 1.1 的保留字体名条款，内部族名改为 "
                 "`Starry Han Sans Light`，未使用 Reserved Font Name `Source`。"
                 "许可证原文见 `assets\\fonts\\OFL.txt`；上游见 <https://github.com/notofonts/noto-cjk>。"),
    },
]

# 随包分发的运行时 DLL（不在 Python 包元数据里，也不是 .tools/bin 下的程序）
BUNDLED_RUNTIME_DLLS = [
    {
        "name": "OpenSSL（libcrypto-3.dll / libssl-3.dll）",
        "version": "3.0.16",
        "license": "Apache-2.0",
        "file": "licenses/OpenSSL-LICENSE.txt",
        "note": "**随本软件分发**（安装目录 `backend\\`）。Python `ssl` 模块使用的 OpenSSL 运行时；未修改其代码。",
    },
    {
        "name": "libffi（libffi-8.dll）",
        "version": "随 Python 3.12",
        "license": "MIT",
        "file": "licenses/libffi-MIT.txt",
        "note": "**随本软件分发**（安装目录 `backend\\`）。Python `ctypes` 使用的 libffi 运行时；未修改其代码。",
    },
    {
        "name": "Intel OpenMP Runtime（libiomp5md.dll）",
        "version": "5.0",
        "license": "Intel Simplified Software License (ISSL)",
        "file": "licenses/Intel-OpenMP-LICENSE.txt",
        "note": "**随本软件分发**（`wheelhouse\\` 的 CTranslate2 wheel 内）。CTranslate2 使用的线程并行运行时；按 ISSL 原样再分发，未修改。",
    },
]

# 随包素材（不由代码许可证覆盖，单独说明授权）
BUNDLED_ASSETS = [
    {
        "name": "默认视频背景（bg-default.mp4 / hero.png）",
        "license": "已获原作者授权（B 站 星雨布丁 channel）",
        "note": "画师：梅原生；动效：Ieno伊诺_Official。授权用于本软件界面背景，随安装包分发；"
                "素材不放入源码仓库。",
    },
    {
        "name": "鼠标光标主题（Assets\\cursors\\）",
        "license": "用户购买的个人授权（不单独分发）",
        "note": "仅随安装包使用，源码仓库不包含这些文件；购买凭证由作者保留。",
    },
]

# 构建工具（不随安装包分发，但产物里含其运行时）
BUILD_TOOLS = [
    {
        "name": "Nuitka",
        "version": "构建工具",
        "license": "AGPL-3.0-or-later + Nuitka Runtime Library Exception 1.0",
        "files": ["licenses/Nuitka-AGPL-3.0.txt", "licenses/Nuitka-RUNTIME-EXCEPTION.txt"],
        "note": "仅用于构建后端。Runtime Library Exception 明确允许把编译产物按自选许可分发；"
                "Nuitka 编译器本身仍为 AGPL-3.0。",
    },
]

# 随包分发的**界面侧**组件（.NET / WinUI 那一堆）——它们不在 Python 包里，扫不出来，得手写
UI_STACK = [
    {"name": "Windows App SDK（WinUI 3）", "license": "Microsoft 软件许可条款（允许随应用分发运行时）",
     "file": "licenses/WindowsAppSDK-LICENSE.txt",
     "note": "**随本软件分发**（程序根目录的 `Microsoft.ui.xaml.dll`、`Microsoft.WindowsAppRuntime.dll` 等）。"
             "界面框架，未修改其代码。"},
    {"name": ".NET 8 运行时（自包含发布）", "license": "MIT",
     "file": "licenses/dotnet-LICENSE.txt",
     "extra": "（另有第三方声明 `licenses/dotnet-ThirdPartyNotices.txt`）",
     "note": "**随本软件分发**（`coreclr.dll`、`System.*.dll` 等）。界面程序的运行时，未修改其代码。"},
    {"name": "Microsoft Visual C++ 运行库", "license": "Microsoft 可再发行组件许可",
     "file": "",
     "note": "**随本软件分发**（`MSVCP140.dll`、`VCRUNTIME140*.dll`）；微软允许随应用一起再发行。"},
    {"name": "Microsoft Edge WebView2（加载器）", "license": "Microsoft 软件许可条款",
     "file": "",
     "note": "**只随包带加载器** `WebView2Loader.dll`；WebView2 运行时本身是系统组件（Windows 11 自带），本软件不分发。"},
]

# 运行时依赖但不由本软件分发（用户自行安装/获取）
RUNTIME_NOTES = [
    ("faster-whisper / Whisper 模型", "MIT",
     "由翻译引擎在首次使用时自动下载，本软件不分发。"),
    ("Qwen3-ASR 模型", "Apache-2.0",
     "由翻译引擎在首次使用时自动下载，本软件不分发。"),
    ("uv（引擎运行时用）", "Apache-2.0 / MIT",
     "引擎本体随包时已带一份 uv 0.11.32（见上面的引擎依赖清单，随包分发）；"
     "运行时另外用到的 uv 由引擎从上游 GitHub（astral-sh/uv）下载，本软件不镜像、不分发。"),
    ("FFmpeg（引擎运行时用，GPL 构建）", "GPL-3.0",
     "由翻译引擎从上游 GitHub（BtbN/FFmpeg-Builds）下载，本软件不镜像、不分发。"
     "**与随包那个 LGPL 构建不是同一份文件**。"),
    ("MinGit（引擎运行时用）", "GPL-2.0",
     "由翻译引擎从上游 GitHub（git-for-windows/git）下载，本软件不镜像、不分发。"),
    ("tokcount（引擎的本地分词器）", "随 finesub（GPL-3.0-or-later）",
     "由翻译引擎从上游 GitHub 下载，本软件不镜像、不分发。"),
    ("BS-Roformer 人声分离模型（权重与 .yaml）", "MIT（依 UVR 的模型声明）",
     "由翻译引擎从上游 GitHub 下载，本软件不镜像、不分发。"
     "模型作者为 Anjok07、aufr33、viperx；代码架构来自 lucidrains/BS-Roformer（MIT）。"
     "UVR README 要求署名，许可证原文见 `licenses/UVR-MIT.txt`。"),
    ("faster-whisper large-v3-turbo 模型权重", "MIT",
     "由翻译引擎从 HuggingFace 下载，本软件不镜像、不分发。"),
    ("whisper-ja-1.5B-ct2（日语重解码）", "上游未声明许可证",
     "由翻译引擎从 HuggingFace 下载，本软件不镜像、不分发。"
     "上游模型卡没有声明许可证，商业使用前请向作者确认。"),
    ("DiariZen 说话人分离模型", "CC-BY-NC-4.0",
     "由翻译引擎按需下载，本软件不镜像、不分发。**非商业许可**；"
     "当前 `--diarize` 默认关闭，开启后用于商业用途前需另行取得授权。"),
    ("DiariZen 导出的 CUDA/CPU TorchScript", "CC-BY-NC-4.0",
     "由翻译引擎按需下载，本软件不镜像、不分发；继承 DiariZen 模型的非商业限制。"),
    ("WeSpeaker 声纹模型", "CC-BY-4.0",
     "由翻译引擎按需下载，本软件不镜像、不分发；使用时需保留署名。"),
    ("Nemotron-3-Diarization", "OpenMDW-1.1",
     "由翻译引擎按需下载，本软件不镜像、不分发；启用说话人分离时使用，"
     "商业使用前请按 OpenMDW-1.1 条款确认。"),
    ("IndexTTS-2.5（本地语音克隆，可选的备用音色）", "bilibili 模型使用许可",
     "**本软件不打包、不分发此组件**：用户在设置里点「下载并启用」时，由本软件"
     "从官方渠道（GitHub / HuggingFace）下载到本机数据目录，随附其原始许可协议；"
     "免费使用、可商用：许可 §2.1 是 worldwide / royalty-free 的完整授权，没有非商业限制，"
     "只有规模门槛：月活超过 1 亿**或**年收入超过 1 亿人民币才需要另行申请书面许可（§2.2）。"
     "**注意：英文版许可写的是 1 billion，和中文版 1 亿不一致，这里按更严格的 1 亿理解。**"
     "另外要求保留版权声明与许可原文（§3.4b）、不得用于训练或改进其它 AI 模型（§3.4c）、"
     "不得用于医疗诊断等高风险场景（§4.2）。"),
    ("MiMo / DeepSeek / Gemini API", "商业服务",
     "由用户自行申请账号与 API Key，并遵守各服务商条款。"),
    ("背景壁纸（t.alcy.cc 等第三方图源）", "第三方内容",
     "设置里的动态/静态壁纸由本软件**在用户机器上实时从第三方图源获取**，本软件不分发、不转存这些图片；"
     "图片版权归原作者所有，仅供个人本地使用。不想用时可在「设置 → 背景壁纸」切回内置的星空底图。"),
]


# ★★ 本软件**镜像托管**在对象存储上的文件（2026-10-17 起为空）
#
# 用户 2026-10-17 决定不再维护国内对象存储，全部改走 GitHub：
# 引擎运行时所需的 uv / FFmpeg GPL 构建 / MinGit / tokcount / BS-Roformer
# 由引擎从上游 GitHub 下载，本软件不镜像、不分发。
MIRRORED: list[tuple[str, str, str]] = []

# 元数据里没写许可证的，用已知信息补齐
KNOWN_LICENSES = {"clr_loader": "MIT", "pythonnet": "MIT", "pywebview": "BSD-3-Clause",
                  "socksio": "MIT"}
EXTRA_LICENSE_FILES = {"socksio": [ROOT / "licenses" / "socksio-MIT.txt"]}


def _license_of(dist_name: str) -> str:
    if dist_name in KNOWN_LICENSES:
        return KNOWN_LICENSES[dist_name]
    try:
        meta = md.metadata(dist_name)
    except Exception:
        return "（未在元数据中找到，见该项目主页）"
    lic = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if not lic or len(lic) > 80:
        lics = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                if c.startswith("License")]
        lic = " / ".join(sorted(set(lics))) if lics else ""
    return lic or "（见该项目主页）"


def _binary_version(item: dict) -> str:
    """版本号直接问程序本身——免得清单里写的和实际发出去的二进制对不上"""
    exe = ROOT / ".tools" / "bin" / item["exe"]
    if not exe.exists():
        return "(未随包)"
    try:
        out = subprocess.run([str(exe), item["arg"]],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return "?"
    first = (out.splitlines() or [""])[0]
    m = re.search(r"version (\S+)", first)
    return (m.group(1) if m else first.strip()) or "?"


def _engine_wheel_rows() -> list[tuple[str, str, str, Path]]:
    """随包的翻译引擎及其依赖：(包名, 版本, 许可证, wheel 路径)"""
    rows: list[tuple[str, str, str, Path]] = []
    if not ENGINE_WHL.is_dir():
        return rows
    for w in sorted(ENGINE_WHL.glob("*.whl")):
        try:
            with zipfile.ZipFile(w) as z:
                meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), "")
                if not meta:
                    continue
                text = z.read(meta).decode("utf-8", "replace")
        except (OSError, zipfile.BadZipFile):
            continue
        name = re.search(r"^Name: (.+)$", text, re.M)
        ver = re.search(r"^Version: (.+)$", text, re.M)
        lic = re.search(r"^License-Expression: (.+)$", text, re.M)
        if not lic:
            lic = re.search(r"^License: (.+)$", text, re.M)
        if not lic or len(lic.group(1)) > 80:
            cls = [c.split("::")[-1].strip() for c in re.findall(r"^Classifier: License :: (.+)$", text, re.M)]
            value = " / ".join(sorted(set(cls))) if cls else "见许可证原文"
        else:
            value = lic.group(1).strip()
        rows.append((name.group(1).strip() if name else w.stem,
                     ver.group(1).strip() if ver else "?",
                     value, w))
    return rows


def _write_engine_license_texts(rows: list[tuple[str, str, str, Path]]) -> None:
    """把随包引擎及各依赖 wheel 里的许可证原文汇总成一个文件"""
    ENGINE_LICENSES.parent.mkdir(parents=True, exist_ok=True)
    chunks = [
        "翻译引擎及其 Python 依赖的许可证原文",
        "由 make_licenses.py 从随包的 engine_whl\\ 各 wheel 中收集。",
        "",
    ]
    for name, ver, lic, wheel in rows:
        chunks.append(f"{'=' * 72}\n{name} {ver} | {lic}\n{'=' * 72}")
        try:
            with zipfile.ZipFile(wheel) as z:
                files = []
                for n in z.namelist():
                    low = n.lower()
                    leaf = low.rsplit("/", 1)[-1]
                    if (leaf.startswith(("license", "copying", "notice", "copyright"))
                            or "/licenses/" in low):
                        files.append(n)
                for n in files:
                    try:
                        text = z.read(n).decode("utf-8", "replace").strip()
                    except (OSError, KeyError):
                        continue
                    if text:
                        chunks.append(f"--- {n} ---\n{text}\n")
        except (OSError, zipfile.BadZipFile):
            continue
    ENGINE_LICENSES.write_text("\n".join(chunks), encoding="utf-8")
    print(f"[OK] 已写 {ENGINE_LICENSES.name}（{len(rows)} 个引擎依赖包）")


def _wheel_version(item: dict) -> str:
    """版本从随包的文件名里读——免得清单写的和实际发出去的对不上

    同一个包可能带多个引擎版本对应的构建（例如 ctranslate2 的 0.4.0 / 0.6.0），
    全部列出来，别只报第一份。
    """
    versions: list[str] = []
    for f in sorted((ROOT / "wheelhouse").glob(item["pattern"])):
        parts = f.name.split("-")
        if len(parts) > 1 and parts[1] not in versions:
            versions.append(parts[1])
    return " / ".join(versions) or "(未随包)"


def _scan_flat_dir(root: Path) -> set[str]:
    """扫一个平铺目录里的顶层包名（包目录 + .pyd），再反查成发行包名。"""
    p2d = md.packages_distributions()
    out: set[str] = set()
    for it in root.iterdir():
        if it.is_file() and it.suffix != ".pyd":
            continue                       # .dll / .exe 之类不是 Python 包
        top = it.stem if it.is_file() else it.name
        dists = p2d.get(top) or []
        for d in dists:
            out.add(d)
        if not dists and it.is_dir() and (it / "__init__.py").exists():
            out.add(top)
    return out


def _scan_nuitka_modules(build: Path) -> set[str]:
    """Nuitka 编过的每个模块在构建目录里都留一个 `module.<点号名>.c` —— 拿它反查发行包名。

    这是唯一能拿到「纯 Python 模块」的途径：它们被编译进 exe 了，
    app.dist 目录里一个文件都不对应。
    """
    p2d = md.packages_distributions()
    out: set[str] = set()
    for f in build.glob("module.*.c"):
        top = f.name[len("module."):-len(".c")].split(".")[0]
        for d in p2d.get(top) or []:
            out.add(d)
    return out


def _license_files(dist_name: str) -> list[Path]:
    """找出一个发行包元数据里带的 LICENSE / COPYING / NOTICE 文件。"""
    try:
        dist = md.distribution(dist_name)
    except Exception:  # noqa: BLE001
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for f in dist.files or []:
        s = str(f).replace("\\", "/")
        low = s.lower()
        leaf = low.rsplit("/", 1)[-1]
        if not (leaf.startswith(("license", "copying", "notice", "copyright"))
                or "/licenses/" in low):
            continue
        p = Path(dist.locate_file(f))
        try:
            if not p.is_file() or p.stat().st_size > 1_500_000:
                continue
        except OSError:
            continue
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _write_package_license_texts(rows: list[tuple[str, str, str]]) -> None:
    """把打包进来的 Python 包的许可证原文汇总到一个文件，随安装包分发。"""
    COMBINED.parent.mkdir(parents=True, exist_ok=True)
    chunks = [
        "Python 打包组件的许可证原文汇总",
        "由 make_licenses.py 从构建环境的包元数据中收集。",
        "",
    ]
    missing: list[str] = []
    for name, ver, lic in rows:
        files = _license_files(name)
        if not files:
            files = EXTRA_LICENSE_FILES.get(name, [])
        if not files:
            missing.append(name)
            continue
        chunks.append(f"{'=' * 72}\n{name} {ver} | {lic}\n{'=' * 72}")
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                chunks.append(f"--- {p.name} ---\n{text}\n")
    if missing:
        chunks.append("以下包未在本地元数据中找到许可证文件，请见各自项目主页：")
        chunks.extend(f"- {n}" for n in missing)
        chunks.append("")
    COMBINED.write_text("\n".join(chunks), encoding="utf-8")
    print(f"[OK] 已写 {COMBINED.name}（{len(rows) - len(missing)} 个包有原文，"
          f"{len(missing)} 个缺文件）")


def main() -> int:
    if NUITKA_BUILD.is_dir() and NUITKA_DIST.is_dir():
        bundled = _scan_nuitka_modules(NUITKA_BUILD) | _scan_flat_dir(NUITKA_DIST)
        print(f"扫描 Nuitka 产物（{NUITKA_BUILD.name} 的模块清单 + {NUITKA_DIST.name} 的包目录）")
    elif PYI_INTERNAL.is_dir():
        bundled = _scan_flat_dir(PYI_INTERNAL)
        print("扫描 PyInstaller 产物（_internal）")
    else:
        print("找不到打包产物，请先运行 python build.py")
        return 1

    rows = []
    for dist in sorted(bundled, key=str.lower):
        try:
            ver = md.version(dist)
        except Exception:
            ver = "?"
        rows.append((dist, ver, _license_of(dist)))

    _write_package_license_texts(rows)
    engine_rows = _engine_wheel_rows()
    if engine_rows:
        _write_engine_license_texts(engine_rows)

    lines = [
        "# 第三方开源组件许可证清单",
        "",
        "本软件（星译）以 GPL-3.0 开源，打包与运行过程中使用了以下第三方开源组件。",
        "我们在此致谢并按各自许可证的要求列明其版权与许可信息。",
        "",
        "## 一、随本软件打包分发的组件",
        "",
        "| 组件 | 版本 | 许可证 |",
        "| --- | --- | --- |",
    ]
    for name, ver, lic in rows:
        lines.append(f"| {name} | {ver} | {lic} |")
    # 只提真在包里的那几个 —— 别写一句「Pillow 为 HPND」，而我们根本没分发 Pillow
    _notes = ["Python 本体采用 PSF 许可证（`licenses/Python-PSF.txt`）"]
    _notes.append("Python 包（含 MCP 及其依赖）的许可证原文汇总见 "
                  "`licenses/PYTHON-PACKAGES-LICENSES.txt`")
    if "Pillow" in bundled:
        _notes.append("Pillow 为 HPND（`licenses/Pillow-HPND.txt`）")
    if "cryptography" in bundled:
        _notes.append("cryptography 为 Apache-2.0 / BSD-3-Clause 双许可（两份原文都在 `licenses\\`）")
    lines += [
        "",
        "> 完整许可证原文随包放在 `licenses\\` 目录里（安装目录下同名文件夹）；"
        "各自的官方仓库也能查到（`python -m pip show <包名>` 可看本地元数据）。",
        "> " + "；".join(_notes) + "。",
        "",
        "## 二、随本软件分发的外部程序",
        "",
        "这些程序不是 Python 库，以独立可执行文件随安装包一起分发（位于程序目录的 `bin\\`），",
        "本软件通过命令行方式调用它们，不修改其代码：",
        "",
        "| 组件 | 版本 | 许可证 |",
        "| --- | --- | --- |",
    ]
    for it in BUNDLED_BINARIES:
        lines.append(f"| {it['name']} | {_binary_version(it)} | {it['license']} |")
    for it in BUNDLED_BINARIES:
        lines.append(f"- **{it['name']}**：{it['note']}")
    if BUNDLED_WHEELS:
        lines += [
            "",
            "另外，翻译引擎首次运行需要的一个 Python 包只发布在 GitHub 上（没有代理的机器",
            "下不动，会导致引擎装不上），因此也随安装包分发一份原样副本（位于 `wheelhouse\\`）：",
            "",
            "| 组件 | 版本 | 许可证 |",
            "| --- | --- | --- |",
        ]
        for it in BUNDLED_WHEELS:
            lines.append(f"| {it['name']} | {_wheel_version(it)} | {it['license']} |")
        for it in BUNDLED_WHEELS:
            lines.append(f"- **{it['name']}**：{it['note']}")
    if engine_rows:
        lines += [
            "",
            "### 翻译引擎及其 Python 依赖（随本软件分发，位于 `engine_whl\\`）",
            "",
            "| 组件 | 版本 | 许可证 |",
            "| --- | --- | --- |",
        ]
        for name, ver, lic, _wheel in engine_rows:
            lines.append(f"| {name} | {ver} | {lic} |")
        lines += [
            "- **finesub（翻译引擎）**：GPL-3.0-or-later。随包的是 PyPI 上**未修改的纯 Python wheel**，"
            "wheel 本身即完整源码；上游源码见 <https://github.com/caca2331/finesub>。"
            "安装引擎时优先从这份本地 wheelhouse 离线安装，失败再走网络。",
            "- 其余依赖均为宽松许可（MIT / BSD / Apache-2.0 / MPL-2.0 / PSF-2.0 等），"
            "许可证原文汇总见 `licenses/ENGINE-WHEELS-LICENSES.txt`。",
        ]
    if BUNDLED_FONTS:
        lines += [
            "",
            "随包的字体（位于程序目录的 `assets\\fonts\\`）：",
            "",
            "| 组件 | 许可证 |",
            "| --- | --- |",
        ]
        for it in BUNDLED_FONTS:
            lines.append(f"| {it['name']} | {it['license']} |")
        for it in BUNDLED_FONTS:
            lines.append(f"- **{it['name']}**：{it['note']}")
    if BUNDLED_RUNTIME_DLLS:
        lines += [
            "",
            "随包分发的运行时 DLL（由 Python / CTranslate2 带入）：",
            "",
            "| 组件 | 版本 | 许可证 |",
            "| --- | --- | --- |",
        ]
        for it in BUNDLED_RUNTIME_DLLS:
            lines.append(f"| {it['name']} | {it['version']} | {it['license']} |")
        for it in BUNDLED_RUNTIME_DLLS:
            lines.append(f"- **{it['name']}**：{it['note']} 许可证原文见 `{it['file']}`。")
    if BUNDLED_ASSETS:
        lines += [
            "",
            "## 二·五、随包素材（不适用代码许可证）",
            "",
            "| 素材 | 授权 |",
            "| --- | --- |",
        ]
        for it in BUNDLED_ASSETS:
            lines.append(f"| {it['name']} | {it['license']} |")
        for it in BUNDLED_ASSETS:
            lines.append(f"- **{it['name']}**：{it['note']}")
    lines += [
        "",
        "## 三、随本软件分发的界面侧组件",
        "",
        "3.0 起的界面是 WinUI 3（自包含发布），安装目录里带的这些微软组件同样是随包分发的：",
        "",
        "| 组件 | 许可证 |",
        "| --- | --- |",
    ]
    for it in UI_STACK:
        lines.append(f"| {it['name']} | {it['license']} |")
    for it in UI_STACK:
        tail = f" 许可证原文见 `{it['file']}`。{it.get('extra', '')}" if it.get("file") else ""
        lines.append(f"- **{it['name']}**：{it['note']}{tail}")
    lines += [
        "",
        "## 四、构建工具",
        "",
        "| 工具 | 许可证 | 说明 |",
        "| --- | --- | --- |",
    ]
    for it in BUILD_TOOLS:
        files = "、".join(f"`{f}`" for f in it.get("files", []))
        lines.append(f"| {it['name']} | {it['license']} | {it['note']} 许可证原文见 {files}。 |")
    lines += [
        "",
        "## 五、运行时依赖，但**不由本软件分发**",
        "",
        "| 组件 | 许可证 | 说明 |",
        "| --- | --- | --- |",
    ]
    for name, lic, note in RUNTIME_NOTES:
        lines.append(f"| {name} | {lic} | {note} |")
    lines += [
        "",
        "## 六、本软件不再镜像托管第三方文件",
        "",
        "用户 2026-10-17 决定不再维护国内对象存储。翻译引擎首次运行时所需的 uv、"
        "FFmpeg（GPL 构建）、MinGit、tokcount、BS-Roformer 模型由引擎从上游 GitHub 下载，"
        "本软件不镜像、不分发。",
        "",
        "## 七、说明",
        "",
        "- **安装包内**随包分发了翻译引擎 finesub（GPL-3.0-or-later）：分发的是 PyPI 上**未修改的纯 Python wheel**，"
        "wheel 内含完整源码，上游源码见 <https://github.com/caca2331/finesub>；本软件自身也以 GPL-3.0 开源。"
        "随包分发的 FFmpeg 为 **LGPL 构建**"
        "（不含 libx264/libx265 等 GPL 部分），按 LGPL 的要求附上许可证原文并允许用户替换 `bin\\` 下的文件。",
        "- 翻译引擎首次运行时所需的第三方文件（FFmpeg GPL 构建 / MinGit / tokcount / BS-Roformer 模型）"
        "由引擎从上游 GitHub 下载，本软件不镜像、不分发；版权与许可归各自作者所有。",
        "- 随包的「思源黑体 Light」是 OFL-1.1 的静态 Light 实例，许可证原文在 `assets\\fonts\\OFL.txt`；"
        "字幕字体固定为这一款，界面不再提供其它字体选项，也不随包分发其它字体。",
        "- 若你二次分发本软件，请一并保留本清单与 `licenses\\` 目录。",
        "",
        f"（本清单由 `make_licenses.py` 自动生成，共 {len(rows)} 个打包组件 + {len(BUNDLED_BINARIES)} 个随包外部程序 + {len(BUNDLED_RUNTIME_DLLS)} 个随包运行时 DLL + {len(BUNDLED_FONTS)} 个随包字体 + {len(engine_rows)} 个翻译引擎依赖包）",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] 已生成 {OUT.name}（{len(rows)} 个打包组件）")
    for name, ver, lic in rows:
        print(f"   - {name} {ver}: {lic[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
