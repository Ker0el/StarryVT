# 第三方开源组件许可证清单

本软件（星空AI视频翻译）为闭源软件，但打包与运行过程中使用了以下第三方开源组件。
我们在此致谢并按各自许可证的要求列明其版权与许可信息。

## 一、随本软件打包分发的组件

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| clr_loader | 0.3.1 | MIT |
| cryptography | 47.0.0 | Apache-2.0 OR BSD-3-Clause |
| MarkupSafe | 2.1.5 | BSD-3-Clause |
| Pillow | 9.5.0 | HPND |
| pythonnet | 3.1.0 | MIT |
| pywebview | 6.2.1 | BSD-3-Clause |

> 完整许可证原文见各组件的官方仓库（如 `python -m pip show <包名>` 可查看本地元数据）。
> Python 本体采用 PSF 许可证；WebView2 运行时为微软系统组件（Windows 11 自带），本软件不随包分发。

## 二、随本软件分发的外部程序

这些程序不是 Python 库，以独立可执行文件随安装包一起分发（位于程序目录的 `bin\`），
本软件通过命令行方式调用它们，不修改其代码：

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| FFmpeg（ffmpeg.exe / ffprobe.exe 及同目录 av*.dll） | n9.0.2-12-gc867e13549-20260927 | LGPL-3.0-or-later |
| yt-dlp（yt-dlp.exe） | 2026.08.19 | Unlicense（公有领域） |
- **FFmpeg（ffmpeg.exe / ffprobe.exe 及同目录 av*.dll）**：**随本软件分发**。刻意选用 LGPL 构建（不含 GPL 的 libx264/libx265），以独立进程方式调用、未修改其代码，用户可自行替换。许可证原文见 `licenses/LGPL-3.0.txt` 与 `licenses/GPL-3.0.txt`；对应源码见 <https://ffmpeg.org/download.html>，本构建的打包脚本见 <https://github.com/BtbN/FFmpeg-Builds>。
- **yt-dlp（yt-dlp.exe）**：**随本软件分发**。许可证原文见 `licenses/yt-dlp-Unlicense.txt`；源码见 <https://github.com/yt-dlp/yt-dlp>。

另外，翻译引擎首次运行需要的一个 Python 包只发布在 GitHub 上（没有代理的机器
下不动，会导致引擎装不上），因此也随安装包分发一份原样副本（位于 `wheelhouse\`）：

| 组件 | 版本 | 许可证 |
| --- | --- | --- |
| CTranslate2（翻译引擎用的推理库） | 4.8.1+finesub0.4.0.cu128 | MIT |
- **CTranslate2（翻译引擎用的推理库）**：**随本软件分发**（位于程序目录的 `wheelhouse\`）。上游为 OpenNMT 的 CTranslate2（MIT）：<https://github.com/OpenNMT/CTranslate2>。这里分发的是翻译引擎所需的 CUDA 构建副本，原样未改；该包只在 GitHub 上发布，为保证没有代理的用户也能装好翻译引擎而随包提供。

## 三、运行时依赖，但**不由本软件分发**

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| finesub（翻译引擎） | GPL-3.0-or-later | **本软件不打包、不分发此组件**；由用户在自己的电脑上通过官方渠道自行安装，本软件仅以命令行方式调用。 |
| faster-whisper / Whisper 模型 | MIT | 由翻译引擎在首次使用时自动下载，本软件不分发。 |
| Qwen3-ASR 模型 | Apache-2.0 | 由翻译引擎在首次使用时自动下载，本软件不分发。 |
| BS-Roformer（人声分离模型） | MIT | 由翻译引擎在首次使用时自动下载，本软件不分发。 |
| MiMo / DeepSeek / Gemini API | 商业服务 | 由用户自行申请账号与 API Key，并遵守各服务商条款。 |

## 四、说明

- 本软件**不包含**任何 GPL 许可的代码，也不打包分发 GPL 组件；随包分发的 FFmpeg 为 **LGPL 构建**（不含 libx264/libx265 等 GPL 部分），按 LGPL 的要求附上许可证原文并允许用户替换 `bin\` 下的文件。
  翻译引擎（finesub，GPL-3.0）由用户在自己电脑上通过官方渠道安装，本软件仅以独立进程方式调用，
  不构成对其源码的修改或再分发。
- 若你二次分发本软件，请一并保留本清单与 `licenses\` 目录。

（本清单由 `make_licenses.py` 自动生成，共 6 个打包组件 + 2 个随包外部程序）
