# 第三方开源组件许可证清单

本软件（星译）以 GPL-3.0 开源，打包与运行过程中使用了以下第三方开源组件。
我们在此致谢并按各自许可证的要求列明其版权与许可信息。

## 一、随本软件打包分发的组件

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| attrs | 26.1.0 | MIT |
| backports.zstd | 1.8.0 | PSF-2.0 |
| bottle | 0.13.4 | MIT |
| brotli | 1.2.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.4.9 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| clr_loader | 0.3.1 | MIT |
| colorama | 0.4.6 | BSD License |
| cryptography | 47.0.0 | Apache-2.0 OR BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx-sse | 0.4.3 | MIT |
| idna | 3.18 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD License |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| Mako | 1.4.1 | MIT |
| markdown-it-py | 4.2.0 | MIT License |
| MarkupSafe | 2.1.5 | BSD-3-Clause |
| mcp | 1.30.0 | MIT |
| mdurl | 0.1.2 | MIT License |
| protobuf | 7.35.1 | 3-Clause BSD License |
| proxy_tools | 0.1.0 | MIT |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.4 | MIT |
| Pygments | 2.20.0 | BSD-2-Clause |
| PyJWT | 2.13.0 | MIT |
| pyOpenSSL | 26.1.0 | Apache License, Version 2.0 |
| PySocks | 1.7.1 | BSD |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pythonnet | 3.1.0 | MIT |
| pywebview | 6.2.1 | BSD-3-Clause |
| pywin32 | 312 | PSF |
| PyYAML | 6.0.3 | MIT |
| referencing | 0.37.0 | MIT |
| requests | 2.33.1 | Apache-2.0 |
| rpds-py | 2026.6.3 | MIT |
| shellingham | 1.5.4 | ISC License |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| socksio | 1.0.0 | UNKNOWN |
| sse-starlette | 3.4.8 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| typer | 0.27.1 | MIT |
| typing-inspection | 0.4.3 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.3 | Apache-2.0 |
| ujson | 5.13.0 | BSD-3-Clause AND TCL |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.52.3 | BSD-3-Clause |
| websockets | 11.0.3 | BSD-3-Clause |
| wsproto | 1.3.2 | MIT |
| zstandard | 0.25.0 | BSD-3-Clause |

> 完整许可证原文随包放在 `licenses\` 目录里（安装目录下同名文件夹）；各自的官方仓库也能查到（`python -m pip show <包名>` 可看本地元数据）。
> Python 本体采用 PSF 许可证（`licenses/Python-PSF.txt`）；Python 包（含 MCP 及其依赖）的许可证原文汇总见 `licenses/PYTHON-PACKAGES-LICENSES.txt`；cryptography 为 Apache-2.0 / BSD-3-Clause 双许可（两份原文都在 `licenses\`）。

## 二、随本软件分发的外部程序

这些程序不是 Python 库，以独立可执行文件随安装包一起分发（位于程序目录的 `bin\`），
本软件通过命令行方式调用它们，不修改其代码：

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| FFmpeg（ffmpeg.exe / ffprobe.exe 及同目录 av*.dll） | n9.0.2-27-g7e93014c5b-20261013 | LGPL-3.0-or-later |
| yt-dlp（yt-dlp.exe） | 2026.08.19 | Unlicense（公有领域） |
- **FFmpeg（ffmpeg.exe / ffprobe.exe 及同目录 av*.dll）**：**随本软件分发**。刻意选用 LGPL 构建（不含 GPL 的 libx264/libx265），以独立进程方式调用、未修改其代码，用户可自行替换。许可证原文见 `licenses/LGPL-3.0.txt` 与 `licenses/GPL-3.0.txt`；对应源码见 <https://ffmpeg.org/download.html>，本构建的打包脚本见 <https://github.com/BtbN/FFmpeg-Builds>。
- **yt-dlp（yt-dlp.exe）**：**随本软件分发**。许可证原文见 `licenses/yt-dlp-Unlicense.txt`；源码见 <https://github.com/yt-dlp/yt-dlp>。

另外，翻译引擎首次运行需要的一个 Python 包只发布在 GitHub 上（没有代理的机器
下不动，会导致引擎装不上），因此也随安装包分发一份原样副本（位于 `wheelhouse\`）：

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| CTranslate2（翻译引擎用的推理库） | 4.8.1+finesub0.4.0.cu128 | MIT |
- **CTranslate2（翻译引擎用的推理库）**：**随本软件分发**（位于程序目录的 `wheelhouse\`）。上游为 OpenNMT 的 CTranslate2（MIT）：<https://github.com/OpenNMT/CTranslate2>。这里分发的是翻译引擎所需的 CUDA 构建副本，原样未改；该包只在 GitHub 上发布，为保证没有代理的用户也能装好翻译引擎而随包提供。

随包的字体（位于程序目录的 `assets\fonts\`）：

| 组件 | 许可证 |
| --- | --- |
| 思源黑体 Light（StarryHanSans-Light.ttf） | SIL Open Font License 1.1 |
- **思源黑体 Light（StarryHanSans-Light.ttf）**：**随本软件分发**（位于程序目录的 `assets\fonts\`）。由 Noto Sans SC 可变字体抽取的 Light（wght=300）静态实例；按 OFL 1.1 的保留字体名条款，内部族名改为 `Starry Han Sans Light`，未使用 Reserved Font Name `Source`。许可证原文见 `assets\fonts\OFL.txt`；上游见 <https://github.com/notofonts/noto-cjk>。

## 三、随本软件分发的界面侧组件

3.0 起的界面是 WinUI 3（自包含发布），安装目录里带的这些微软组件同样是随包分发的：

| 组件 | 许可证 |
| --- | --- |
| Windows App SDK（WinUI 3） | Microsoft 软件许可条款（允许随应用分发运行时） |
| .NET 8 运行时（自包含发布） | MIT |
| Microsoft Visual C++ 运行库 | Microsoft 可再发行组件许可 |
| Microsoft Edge WebView2（加载器） | Microsoft 软件许可条款 |
- **Windows App SDK（WinUI 3）**：**随本软件分发**（程序根目录的 `Microsoft.ui.xaml.dll`、`Microsoft.WindowsAppRuntime.dll` 等）。界面框架，未修改其代码。 许可证原文见 `licenses/WindowsAppSDK-LICENSE.txt`。
- **.NET 8 运行时（自包含发布）**：**随本软件分发**（`coreclr.dll`、`System.*.dll` 等）。界面程序的运行时，未修改其代码。 许可证原文见 `licenses/dotnet-LICENSE.txt`。（另有第三方声明 `licenses/dotnet-ThirdPartyNotices.txt`）
- **Microsoft Visual C++ 运行库**：**随本软件分发**（`MSVCP140.dll`、`VCRUNTIME140*.dll`）；微软允许随应用一起再发行。
- **Microsoft Edge WebView2（加载器）**：**只随包带加载器** `WebView2Loader.dll`；WebView2 运行时本身是系统组件（Windows 11 自带），本软件不分发。

## 四、运行时依赖，但**不由本软件分发**

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| finesub（翻译引擎） | GPL-3.0-or-later | **本软件不打包、不分发此组件**；首次使用时在用户自己的电脑上从上游渠道安装，本软件仅以独立进程方式调用。 |
| faster-whisper / Whisper 模型 | MIT | 由翻译引擎在首次使用时自动下载，本软件不分发。 |
| Qwen3-ASR 模型 | Apache-2.0 | 由翻译引擎在首次使用时自动下载，本软件不分发。 |
| uv（引擎运行时用，两个版本） | Apache-2.0 / MIT | 由翻译引擎从上游 GitHub（astral-sh/uv）下载，本软件不镜像、不分发。 |
| FFmpeg（引擎运行时用，GPL 构建） | GPL-3.0 | 由翻译引擎从上游 GitHub（BtbN/FFmpeg-Builds）下载，本软件不镜像、不分发。**与随包那个 LGPL 构建不是同一份文件**。 |
| MinGit（引擎运行时用） | GPL-2.0 | 由翻译引擎从上游 GitHub（git-for-windows/git）下载，本软件不镜像、不分发。 |
| tokcount（引擎的本地分词器） | 随 finesub（GPL-3.0-or-later） | 由翻译引擎从上游 GitHub 下载，本软件不镜像、不分发。 |
| BS-Roformer 人声分离模型（权重与 .yaml） | MIT（依 UVR 的模型声明） | 由翻译引擎从上游 GitHub 下载，本软件不镜像、不分发。模型作者为 Anjok07、aufr33、viperx；代码架构来自 lucidrains/BS-Roformer（MIT）。 |
| IndexTTS-2.5（本地语音克隆，可选的备用音色） | bilibili 模型使用许可 | **本软件不打包、不分发此组件**：用户在设置里点「下载并启用」时，由本软件从官方渠道（GitHub / HuggingFace）下载到本机数据目录，随附其原始许可协议；免费使用、可商用：许可 §2.1 是 worldwide / royalty-free 的完整授权，没有非商业限制，只有规模门槛：月活超过 1 亿**或**年收入超过 10 亿人民币才需要另行申请书面许可（§2.2）。另外要求保留版权声明与许可原文（§3.4b）、不得用于训练或改进其它 AI 模型（§3.4c）、不得用于医疗诊断等高风险场景（§4.2）。 |
| MiMo / DeepSeek / Gemini API | 商业服务 | 由用户自行申请账号与 API Key，并遵守各服务商条款。 |
| 背景壁纸（t.alcy.cc 等第三方图源） | 第三方内容 | 设置里的动态/静态壁纸由本软件**在用户机器上实时从第三方图源获取**，本软件不分发、不转存这些图片；图片版权归原作者所有，仅供个人本地使用。不想用时可在「设置 → 背景壁纸」切回内置的星空底图。 |

## 五、本软件不再镜像托管第三方文件

用户 2026-10-17 决定不再维护国内对象存储。翻译引擎首次运行时所需的 uv、FFmpeg（GPL 构建）、MinGit、tokcount、BS-Roformer 模型由引擎从上游 GitHub 下载，本软件不镜像、不分发。

## 六、说明

- **安装包内**不含任何 GPL 许可的代码、也不打包 GPL 组件；随包分发的 FFmpeg 为 **LGPL 构建**（不含 libx264/libx265 等 GPL 部分），按 LGPL 的要求附上许可证原文并允许用户替换 `bin\` 下的文件。
  翻译引擎（finesub，GPL-3.0-or-later）不随安装包分发，首次使用时在用户自己的电脑上从上游渠道
  安装，本软件仅以独立进程方式调用、不修改其源码（GPLv3 第 9 条：运行一份副本本身不需要接受该许可，也不产生义务）。
- 翻译引擎首次运行时所需的第三方文件（uv / FFmpeg GPL 构建 / MinGit / tokcount / BS-Roformer 模型）由引擎从上游 GitHub 下载，本软件不镜像、不分发；版权与许可归各自作者所有。
- 随包的「思源黑体 Light」是 OFL-1.1 的静态 Light 实例，许可证原文在 `assets\fonts\OFL.txt`；字幕字体固定为这一款，界面不再提供其它字体选项，也不随包分发其它字体。
- 若你二次分发本软件，请一并保留本清单与 `licenses\` 目录。

（本清单由 `make_licenses.py` 自动生成，共 60 个打包组件 + 2 个随包外部程序 + 1 个随包字体）
