<div align="center">
  <img src="assets/banner.png" alt="星译 StarryVT，本地视频翻译工具：AI 字幕 + 中文配音" width="100%">
</div>

<h1 align="center">星译 · StarryVT</h1>

<h3 align="center"><i>Easy Translate</i></h3>

<p align="center">
  <b>一键部署，自动生成翻译和字幕</b><br>
  视频 → 中文字幕 → 中文配音 → 成片，全程在本机完成，<b>视频不上传</b>
</p>

<p align="center">
  <a href="../../releases/latest"><img src="https://img.shields.io/badge/⬇️%20下载最新版-v6.12-2ea44f?style=for-the-badge" alt="下载最新版"></a>
  <img src="https://img.shields.io/badge/平台-Windows%2010%20%7C%2011-0078d4?style=for-the-badge" alt="Windows 10 / 11">
  <img src="https://img.shields.io/badge/翻译引擎-finesub-8957e5?style=for-the-badge" alt="finesub">
  <img src="https://img.shields.io/badge/配音-小米%20MiMo-ff8a3d?style=for-the-badge" alt="小米 MiMo">
  <a href="https://github.com/Ker0el/StarryVT/blob/main/LICENSE"><img src="https://img.shields.io/badge/许可-GPL--3.0-blue?style=for-the-badge" alt="GPL-3.0"></a>
</p>

<p align="center">
  <a href="https://docs.qq.com/doc/DUmpxWE1kVUFXVHpG"><b>📘 使用文档</b></a>
</p>

---

## 📖 这是什么

星译是一款**免费、功能完整的 Windows 本地视频翻译工具**。导入一个视频文件、选择整个文件夹，或粘贴一条 YouTube / B 站链接，它会自动完成**语音识别、AI 翻译、中文字幕生成、配音合成与字幕压制**，一键生成可以直接发布的视频成片。

不用手动整理字幕，也不用在几个软件之间来回倒。

**视频不上传。** 识别、翻译、配音、压制全部在你本机完成；翻译和配音只调用你自己申请的 API Key，不经过作者的服务器，作者看不到你的视频，也看不到你的成品。

## ✨ 核心功能

- **🎬 一键生成翻译和字幕**：自动完成语音识别、AI 翻译、中文字幕生成、中文配音与字幕压制，一次完成。
- **🎨 字幕样式可调**：字体、字号、字间距、描边、颜色、底部距离都能改，设置页里实时预览，点预览图可放大。
- **🔒 本地运行，保护隐私**：视频处理在本机完成，不上传至作者服务器。翻译与配音通过你自己的 API Key 调用服务。
- **🎙️ AI 配音与音色替换**：为外语视频生成中文配音，也可为中文视频重新合成配音。
- **📝 字幕检查与编辑**：翻译完成后暂停，双语对照检查译文，改完再继续配音和压制（默认关闭，可在设置里开启）。
- **🌏 多语言支持**：支持英语、日语、韩语、俄语、德语等语言的视频翻译。
- **📦 批量处理**：支持整个文件夹及子文件夹批量处理，单个任务失败不影响其他任务。
- **🔗 链接直接导入**：支持 YouTube / B 站视频链接，自动获取视频信息并进行处理。
- **🤖 AI Agent 接入**：通过 MCP 让 Claude Code / DeepSeek Harness / Codex / WorkBuddy / Cursor / Trae / 豆包工作 直接启动翻译、查询进度、取消任务（安装包 6.0 起内置）。
- **🆓 免费且功能完整**：不锁定功能、不添加水印、不限制处理次数。

<details>
<summary><b>还有一些在设置里</b></summary>

**输入**
- 视频、文件夹直接拖进窗口
- 合集或整个文件夹一键批量，逐条进度可见

**翻译与配音**
- 有创作者字幕就直接拿来翻，没有才走语音识别，更快也更准
- 小米 MiMo TTS，九种音色，支持一键试听

**字幕样式**
- 字体固定为随包的思源黑体 Light，不用自己装；字号、字间距、描边、颜色、底部距离都能调
- 字号、字间距、描边、颜色、底部距离都能调，设置页实时预览
- 长行自动缩小字号，尽量保持一行
- 可选**本地语音克隆**：用原视频里的人声念中文，不联网、不要 Key
- 原声音量滑杆，0% 是纯中文，默认保留一点背景音，音乐和音效不丢
- 视频里的专有名词会记进**本机术语库**，同一个系列的下一条更准
- 可选「联网查证」与「视频语境纠错」，专业内容更稳

**使用**
- 一键初始化，缺什么自己装，一项一项过给你看
- 内置 FFmpeg 与 yt-dlp，不用自己装
- 背景壁纸默认是随包那条视频，也能换成随机图源、切成静态图，喜欢的可以收藏
- 启动时检查新版本，一键升级（更新包从 GitHub 下载，国内网络可能需要代理）
- 崩溃和错误自动落盘，一键导出诊断报告（不含你的 API Key）

</details>

## ⭐ 为什么选星译

**自动化流程，简单易用，一键部署。**

| 你关心的 | 星译的做法 |
|:--|:--|
| 一键部署 | 一个安装包，点一次【一键初始化】，运行环境和模型自动下齐，不用自己装 Python、CUDA 或 FFmpeg |
| 自动化流程 | 视频进，语音识别、AI 翻译、中文配音、字幕压制自动跑完，输出带中文字幕和中文配音的 MP4 成片 |
| 简单易用 | 文件拖进窗口、文件夹批量、粘贴 YouTube / B 站链接都能开始，翻译、配音、音量、输出都有调好的默认值 |
| 界面清爽 | 现代 WinUI 3 界面，默认深色主题；没有广告、没有推广弹窗，也不捆绑其他软件 |
| 字幕更准 | 视频自带创作者字幕时直接拿来翻译，没有才走语音识别；专有名词还会记进本机术语库 |
| 字幕检查 | 翻译完成后暂停，双语对照检查译文，改完再继续配音和压制（默认关闭，可在设置里开启） |
| 批量处理 | 多条任务排队跑，单条失败不影响其他任务 |
| 出了问题 | 内置更新从 GitHub 下载，一键导出诊断报告，报告不含你的 API Key |

以上功能都包含在内：不加水印、不限次数，翻译和配音只消耗你自己的 API 用量。

## 🔄 工作流程

**语音识别 → AI 翻译 → 配音合成 → 字幕压制 → 输出视频**

最终拿到的是**包含中文字幕与配音的 MP4 成片**，而不只是一份字幕文件。

## 🖥️ 界面

WinUI 3 深色界面，截图和界面说明见[使用文档](https://docs.qq.com/doc/DUmpxWE1kVUFXVHpG)。

## 🚀 快速开始

**① 下载安装**
到 [Releases](../../releases/latest) 下载 `StarryVT-Setup.exe` 双击安装。
**免管理员权限**，装哪个盘自己选。

**② 一键初始化**
打开软件，主页点【一键初始化】。翻译引擎本体已经随安装包带好，这一步会把引擎运行环境和三个模型权重**一次下齐**，
约 6 GB 下载、装完占约 12 GB，带进度带校验。下载一次永久复用，重装软件也不用重下。

**③ 填 API Key**
左侧【设置】里每栏都有「去申请」和「测试」按钮，点一下就知道通没通。
[DeepSeek](https://platform.deepseek.com/api_keys) 是翻译主力，必填；
[小米 MiMo](https://platform.xiaomimimo.com/console/api-keys) 配音用（目前限时免费）。
两个都按量计费，**直接跟服务商结算，不经过作者**。

**④ 开始翻译**
选视频或贴链接，点【开始翻译】。第一次翻译不会再下模型（初始化时已经下齐）。

📘 **图文使用文档**：[星译使用文档（腾讯文档）](https://docs.qq.com/doc/DUmpxWE1kVUFXVHpG)

## 🤖 MCP 接入（AI Agent 调用）

星译可以把本机翻译能力通过 MCP 暴露给 AI Agent，让它直接启动翻译、查看进度、取消任务、查询术语库。MCP 服务和界面共用同一套设置、API Key、引擎与输出目录。

> 当前状态：MCP 服务已内置在 6.0 及更高版本的安装包里，安装版用户不需要安装 Python 或 mcp SDK，只需要配置 MCP 客户端。

### 支持的客户端

只要客户端支持**本地 stdio MCP**，就能接。下面是已经确认有 MCP 入口的客户端：

| 客户端 | 支持情况 | 怎么加 |
|:--|:--|:--|
| Claude Code | ✅ | `claude mcp add ...`，见下 |
| DeepSeek Harness（DSH） | ✅ | 在 DSH 的 MCP 配置里加 stdio server |
| Codex CLI | ✅ | `~/.codex/config.toml` 里加 `[mcp_servers.starryvt]` |
| Cursor | ✅ | Settings → MCP → Add new MCP server，选 stdio |
| Trae | ✅ | 设置 → MCP → 手动添加 |
| WorkBuddy（腾讯） | ✅ | 设置 → MCP → 添加 stdio server |
| 豆包工作 | ✅ | 连接器里选本地 STDIO；只收 HTTP 的版本用下面的本地 HTTP 模式 |
| Claude Desktop / 其它 MCP 客户端 | ✅ | 填 command + args，或对应的 stdio 配置 |

> DeepSeek 和豆包要用它们的 Agent/工作版：DeepSeek Harness（DSH）、豆包工作。普通的聊天 App 没有自定义 MCP 入口。
>
> 支持本地 stdio 的客户端直接按下面的配置接；只收 HTTP 的客户端用「本地 HTTP 模式」。

### 通用配置

大多数客户端用同一份 JSON，填到它的 MCP 配置里：

```json
{
  "mcpServers": {
    "starryvt": {
      "command": "<星译安装目录>\\backend\\StarryVT.exe",
      "args": ["--mcp"]
    }
  }
}
```

默认安装目录是 `%LOCALAPPDATA%\Programs\StarryVT`，装到别的盘就换成实际路径。

### Claude Code

```bash
claude mcp add --transport stdio starryvt -- "<星译安装目录>\backend\StarryVT.exe" --mcp
```

### Codex CLI

在 `C:\Users\<你的用户名>\.codex\config.toml` 里加：

```toml
[mcp_servers.starryvt]
command = "<星译安装目录>\\backend\\StarryVT.exe"
args = ["--mcp"]
```

不同版本的 Codex 也可以试 `codex mcp add`，以 `codex mcp add --help` 为准。

### 本地 HTTP 模式

有些客户端只收 HTTP / Streamable HTTP，不接受本地 stdio。这时先启动星译的本地 HTTP 模式：

```bash
"<星译安装目录>\backend\StarryVT.exe" --mcp-http
```

然后在客户端的 MCP / 连接器里填：

- 传输类型：`HTTP` 或 `Streamable HTTP`
- 服务器 URL：`http://127.0.0.1:24139/mcp`

HTTP 服务只监听 `127.0.0.1`，不会暴露到局域网。这个窗口要一直开着，关掉后 Agent 就调不动星译了。

检查是否连上：

- Claude Code：`claude mcp list`
- 其它客户端：MCP 列表里出现 `starryvt`，或直接问 Agent「你有哪些星译工具」。

### 怎么用

装好后直接对 Agent 说人话即可，例如：

- 「用星译把 `D:\videos\demo.mp4` 翻成中文，出成片。」
- 「只出字幕，不要配音。」
- 「查一下刚才那个任务的进度。」
- 「取消任务。」
- 「在术语库里查一下 MCP。」

Agent 会调用 `starryvt_translate` 启动任务，拿到 `job_id` 后用 `starryvt_status` 轮询，或用 `starryvt_wait` 等待完成；取消用 `starryvt_cancel`，查术语库用 `starryvt_knowledge_search`，环境检查用 `starryvt_env`，标题和背景信息用 `starryvt_probe`。

### 注意事项

- 第一次使用仍要先在星译界面点【一键初始化】，并在设置里填好 API Key；MCP 不会代替这一步。
- 任务使用你自己的 API Key，可能产生费用。
- 一次只跑一个任务；MCP 服务器要在 Agent 会话期间保持运行。
- 目前支持本机 stdio 和本地 HTTP（只监听 `127.0.0.1`），不开放公网访问。

## 🎯 适用场景

- 外语教程、技术课程与知识视频翻译
- 中文教程翻译成英文，给海外观众学习，受众更多
- YouTube / B 站视频本地化
- 外语视频的中文字幕与中文配音制作
- 中文视频的音色替换与配音重制
- 大批量视频翻译与处理

## 🔑 环境要求

| 项目 | 要求 |
|:--|:--|
| 系统 | Windows 10 / 11 x64 |
| 显卡 | 建议 NVIDIA RTX 20 系以上、显存 ≥4 GB；纯 CPU 也能跑，慢一些 |
| 磁盘 | ≥ 20 GB（引擎与模型约 12 GB + 下载缓存约 5 GB）；本地语音克隆再 +15 GB |
| DeepSeek API Key | 翻译主力，必填 |
| 小米 MiMo API Key | 配音用（限时免费） |
| MCP 客户端（可选） | Claude Code / DeepSeek Harness / Codex / Cursor / Trae / WorkBuddy / 豆包工作 等；支持本地 stdio 或本地 HTTP MCP 即可，只在用 AI Agent 调用时需要 |
| 网络代理 | **下载 YouTube 和从 GitHub 检查更新时可能需要**，填本机代理地址如 `http://127.0.0.1:7897`；B 站、翻译、配音不需要 |

## 🌏 支持的语言

- 英语 / 日语 / 韩语 / 俄语 / 德语 **译成中文**
- **中文 → 英语**：中文语音识别 → DeepSeek 翻英文 → MiMo 英文配音，支持原声克隆。英文配音建议选 Mia / Chloe / Milo / Dean
- 中文（换音色重配）：把中文语音用所选音色重新朗读一遍

## 📁 输出在哪

成品默认存在原视频旁边。本地视频或文件夹输入时，在它所在目录下建一个 `output\`：
视频在 `E:\教程\`，成品就在 `E:\教程\output\`。

链接下载没有本地来源，落在软件目录下的 `output\`；设置里填了「输出目录」则一律以设置为准。

单个视频一个文件夹，里面是原片、封面、中文字幕、配音版，以及带中文配音和烧录字幕的
`_final.mp4` 成片。批量翻译时每个视频一个子文件夹。

<details>
<summary><b>那个 tts_cache 文件夹是什么？</b></summary>

AI 配音的逐句音频缓存。重跑同一个视频会秒级复用，不用重新合成配音；确认成片没问题后可以删掉。
</details>

## ❓ 常见问题

<details>
<summary><b>首次为什么要下载这么多东西？</b></summary>

- **一键初始化**（约 6 GB 下载 / 约 12 GB 磁盘）：翻译引擎本体已随安装包带好；这一步下载的是
  引擎运行环境（Python + CUDA 版 torch，约 2.6 GB）和三个模型权重，人声分离、语音识别、第二模型校验（约 3.7 GB）。
  这是**一辈子一次**的，装好后重装软件也不用重下。
- **本地语音克隆**（可选，约 15 GB）：在设置里手动点，不用就永远不用下。

**初始化时一次下齐，是为了避免「第一次翻译时才悄悄下载」**：那个过程没有进度、
下断了也不报错，以前的用户就是在这儿卡住的。
</details>

<details>
<summary><b>没有代理能用吗？</b></summary>

能，而且绝大部分功能都不需要代理：翻译（DeepSeek）、配音（MiMo）、语音识别、
字幕烧录、本地语音克隆、下载 B 站链接，全都可用。

**只有下载 YouTube 链接，以及从 GitHub 检查更新，国内网络可能需要代理**；B 站不需要。
</details>

<details>
<summary><b>可以只要字幕，不要配音吗？</b></summary>

可以。设置 → 翻译与配音 → 默认输出里选「只出字幕」，只会导出一份 SRT；想保留原声、只把字幕烧进视频，选「只加字幕（保留原声）」；选「只出配音」则只生成配音文件；选「下载视频」则只把视频下载到输出目录，不翻译也不配音。
</details>

<details>
<summary><b>背景原声怎么处理？</b></summary>

设置里的「原声音量」滑杆控制。0% 是纯中文，15%（默认）会保留一点背景音，音乐和音效不丢。
</details>

<details>
<summary><b>翻译准确吗？</b></summary>

由 AI 自动生成，**可能存在错误，请务必自行核对后再使用**，详见[使用条款](TERMS.md)。
专业术语多的内容，软件会按视频标题自动认领域并把相关术语带进提示词，通常不用手填；
翻过的专有名词还会留在本机术语库里，同一个系列越翻越准。
</details>

<details>
<summary><b>背景壁纸是哪来的？</b></summary>

随包那条默认视频背景由 B 站[「星雨布丁」](https://www.bilibili.com/video/BV1PaQHBaEsg)授权使用（软件内「关于」页有署名，点一下能到原片）。

其余壁纸来自第三方公开图源，在你本机实时获取、缓存在本地，软件不转存也不再分发；
图片版权归原作者，请勿用于商业用途。不想用可在「设置 → 外观」里换掉。
</details>

<details>
<summary><b>出错了怎么办？</b></summary>

设置里点【导出诊断报告】，把生成的 zip 发给作者。软件会自动记录完整堆栈与环境信息，
**不含你的 API Key**。
</details>

## 🆓 免费使用

星译**完全免费**，功能完整可用，不加水印、无广告、不限次数。

## 📦 源码

源码在本仓库：[Ker0el/StarryVT](https://github.com/Ker0el/StarryVT)，以 **GPL-3.0** 开源。
构建方法见 [BUILDING.md](BUILDING.md)。

> 安装包里随附的默认视频背景、鼠标光标主题等素材不在源码仓库内；
> 它们涉及第三方授权或个人信息，构建时请使用自己的素材或留空。

## 📄 许可与声明

- 本软件以 **GPL-3.0** 开源，源码见本仓库根目录 `LICENSE`；界面基于 Windows App SDK（WinUI 3）。随包分发的第三方组件与外部程序
  （FFmpeg / yt-dlp / CTranslate2 等）的许可证原文与源码地址见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) 与安装目录下的 `licenses\`；
  引擎运行时文件由翻译引擎从上游 GitHub 下载，本软件不镜像、不分发
- FFmpeg 选用 LGPL 构建（不含 GPL 部分），按 LGPL 要求附许可证原文，并允许用户自行替换 `bin\` 下的文件
- 翻译引擎 [finesub](https://github.com/caca2331/finesub)（GPL-3.0-or-later）以 PyPI 上**未修改的纯 Python wheel** 随安装包分发，
  wheel 内含完整源码；运行时环境和模型由引擎在首次使用时从上游下载
- 链接导入仅用于个人学习、翻译与本地观看；作者标注「禁止转载」的作品请勿转载、二次上传或商用
- 使用条款见 [TERMS.md](TERMS.md)。AI 生成结果仅供参考，请自行核对

## 🙏 致谢

感谢以下开源项目：

| 项目 | 在这里做什么 |
|:--|:--|
| [finesub](https://github.com/caca2331/finesub) | 翻译引擎，整条流水线的主干 |
| [FFmpeg](https://ffmpeg.org/) | 音视频处理与字幕烧录 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 下载 YouTube / B 站视频 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [Whisper](https://github.com/openai/whisper) | 没有官方字幕时的语音识别 |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | 识别与翻译的推理加速 |
| [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) | 语音识别模型（第二模型复核） |
| [UVR / BS-Roformer](https://github.com/Anjok07/ultimatevocalremovergui) | 人声与伴奏分离（模型来自 UVR，架构代码来自 lucidrains） |
| [Git for Windows](https://github.com/git-for-windows/git) | 引擎运行时需要的 git（MinGit 由引擎从上游下载） |
| [IndexTTS](https://github.com/index-tts/index-tts) | 本地语音克隆 |
| [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) | AI Agent 接入（MCP） |
| [思源黑体 / Noto Sans SC](https://github.com/notofonts/noto-cjk) | 随包的字幕字体（OFL-1.1，Light 静态实例） |
| [Windows App SDK](https://github.com/microsoft/WindowsAppSDK) / [.NET](https://github.com/dotnet/runtime) | 界面 |

翻译、配音和语音识别实际用到的模型与渠道：

| 环节 | 模型 | 渠道 |
|:--|:--|:--|
| 翻译 | deepseek-flash（失败时自动切 deepseek-v4-pro） | DeepSeek 官方 API |
| 配音 | mimo-v2.5-tts / mimo-v2.5-tts-voiceclone | 小米 MiMo 官方 API |
| 本地语音克隆 | IndexTTS-2.5 | 在本机运行 |
| 语音识别 | faster-whisper large-v3-turbo | 在本机运行 |
| 第二模型复核 | Qwen3-ASR 0.6B | 在本机运行 |

配音的音色来自 [小米 MiMo](https://github.com/XiaomiMiMo)。\
随包那条默认视频背景来自 B 站 [**星雨布丁**](https://www.bilibili.com/video/BV1PaQHBaEsg)（已获授权）。\

- 画师：梅原生
- 动效：Ieno伊诺_Official

「关于」页的版式与入场动效参考自 [DreamTranslatePO](https://github.com/TypeDreamMoon/DreamTranslatePO)。

随包分发的组件与各自的许可证原文，见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

## 👥 贡献者

- **星空（Ker0el）**：作者、维护者

## 💬 联系作者

反馈问题、提建议、定制需求都欢迎，微信：**Star0xcc**

## English

**StarryVT (星译)** is a free, fully functional **local video translation tool for Windows**.
Give it a video file, a folder, or a YouTube / Bilibili link, and one click produces
**translated Chinese subtitles and a Chinese dub**, all the way to a finished MP4:
**speech recognition → AI translation into Chinese → Chinese AI dubbing →
burned-in subtitles → final MP4**.

Your video is **never uploaded**. Recognition, translation, dubbing and muxing all run on
your own machine, and translation / TTS use *your own* API keys (DeepSeek, Xiaomi MiMo).

- Language directions: English / Japanese / Korean / Russian / German → **Chinese**, and **Chinese → English** (Chinese ASR + DeepSeek translation + English TTS)
- One-click setup: the runtime and models are downloaded automatically, no Python / CUDA / FFmpeg setup
- Modern WinUI 3 interface, dark by default: no ads, no promotional pop-ups, no bundled software
- Input: single files, whole folders, YouTube / Bilibili links, batch queues
- MCP server for AI agents (Claude Code / DeepSeek Harness / Codex / Cursor / Trae / WorkBuddy / Doubao Work): start translations, check progress, cancel jobs
- Requires: Windows 10/11 x64, ~20 GB disk, an NVIDIA GPU recommended (CPU works, slower)
- Free and fully functional; the app's UI is in Chinese
- Source code: [github.com/Ker0el/StarryVT](https://github.com/Ker0el/StarryVT), licensed under **GPL-3.0**

**Contributors:** Ker0el (author and maintainer).

---

<p align="center"><sub>Made with ❤️ by Ker0el · Powered by <a href="https://github.com/caca2331/finesub">finesub</a> & 小米 MiMo</sub></p>
