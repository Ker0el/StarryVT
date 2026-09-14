<div align="center">
  <img src="assets/banner.png" alt="星空AI视频翻译" width="100%">
</div>

<h1 align="center">星空AI视频翻译</h1>

<p align="center">
  <b>本地运行的 AI 视频翻译工具</b> · 视频 → 中文翻译 → 中文配音 → 带字幕成片
</p>

<p align="center">
  <img src="https://img.shields.io/badge/版本-0.1.0-7aa2ff?style=flat-square" alt="版本">
  <img src="https://img.shields.io/badge/平台-Windows%2010%2F11-2ea44f?style=flat-square" alt="平台">
  <img src="https://img.shields.io/badge/引擎-finesub-8957e5?style=flat-square" alt="引擎">
  <img src="https://img.shields.io/badge/配音-小米%20MiMo-ff8a3d?style=flat-square" alt="MiMo">
</p>

---

## ✨ 功能

| | 说明 |
|---|---|
| 🎬 **视频翻译** | 粘贴 YouTube 链接或选择本地文件 → 输出带中文字幕的成片 |
| 🎙️ **AI 配音** | 小米 MiMo TTS，**9 种音色**（冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean），支持**一键试听** |
| 📝 **精修字幕** | finesub 引擎：人声分离 → 双模型语音识别 → LLM 纠错翻译，字幕质量拉满 |
| 📦 **批量翻译** | 整个**文件夹**或 YouTube **合集**一键批量，逐条进度可见，单条失败不影响整批 |
| 🖼️ | 自动下载视频封面 |
| 🎚️ | 原声音量可调（**拉到 0% = 纯中文配音**） |
| 🧰 | **一键安装翻译引擎**（自动装 uv + finesub，无需懂命令行） |
| 🩺 | 完整诊断日志：崩溃/错误自动落盘，一键导出报告（不含密钥） |

## 🚀 快速开始

**1. 下载安装** — 到 [Releases](../../releases/latest) 下载 `星空AI视频翻译.exe`，双击安装
> 免管理员权限，装到用户目录；卸载时连同 `output/` 一起清干净

**2. 装翻译引擎** — 打开软件，顶部若提示「未检测到翻译引擎」→ 点【一键安装】

**3. 填 API Key** — 右上角【设置】→ 三栏 Key 都带「去申请 ↗」链接和「**测试**」按钮（点一下就知道通不通）

**4. 开始翻译** — 选视频 / 贴链接 → 点【开始翻译】
> 首次会自动下载模型（约 8.5GB，有进度显示），之后永久复用

## 🔑 准备工作

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64（WebView2 系统自带） |
| 显卡 | 建议 NVIDIA RTX 20 系以上、显存 ≥4GB（纯 CPU 也能跑，慢） |
| 磁盘 | ≥15 GB（模型 + 运行时 + 输出） |
| **MiMo API Key** | 配音用 · [申请地址](https://platform.xiaomimimo.com/console/api-keys) |
| **DeepSeek / Gemini Key** | 翻译用（二选一即可）· [DeepSeek](https://platform.deepseek.com/api_keys) · [Gemini](https://aistudio.google.com/apikey) |
| 网络代理 | 访问 YouTube / Google 需要（设置里填本机代理地址，如 `http://127.0.0.1:7897`） |

## 🌏 支持的语言

**源语言**：英语 / 日语 / 韩语 / 俄语 / 中文 　→　**目标固定为中文**

> 💡 选「中文」= **换音色重配**：把你自己的中文语音，用所选音色（如冰糖）重新朗读

## 📁 输出结构

```
output/<视频名>-<日期时间>/
├── 封面.jpg              视频封面
├── <视频名>.srt          中文字幕（可直接外挂播放器）
├── <视频名>_dubbed.mp4   配音版（无字幕）
└── <视频名>_final.mp4    ⭐ 成片（中文配音 + 字幕烧录）
```

## ❓ 常见问题

<details>
<summary><b>首次为什么要下载 8.5GB？</b></summary>

那是翻译引擎的运行时 + 三个 AI 模型：Whisper-large-v3-turbo（识别）、Qwen3-ASR（交叉校验）、BS-Roformer（人声分离）。下载一次永久复用，重装软件也不用重下。
</details>

<details>
<summary><b>必须开代理吗？</b></summary>

**翻译和下载需要**（要访问 Google / YouTube）；语音识别、配音、烧字幕全部在本机完成，不需要。
</details>

<details>
<summary><b>可以只要字幕，不要配音吗？</b></summary>

可以。主页有「生成中文配音」「烧录字幕」两个开关，按需勾选。
</details>

<details>
<summary><b>配音能只换一部分、原声保留多少？</b></summary>

主页的「**原声音量**」滑杆控制：0% = 纯中文（一点原声都没有），数值越大背景原声越明显。
</details>

<details>
<summary><b>出错了怎么办？</b></summary>

【设置】→【导出诊断报告】，把生成的 zip 发给作者即可。软件会自动记录完整堆栈与环境信息（**不含你的 API Key**）。
</details>

<details>
<summary><b>翻译结果准确吗？</b></summary>

AI 自动生成，**可能存在错误，请务必自行核对后再使用**（详见[使用条款](TERMS.md)）。
</details>

## 📄 许可与声明

- 本软件为**闭源软件**；打包内置的第三方开源组件（pywebview / pythonnet 等，全部为宽松许可）见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
- 使用条款见 [TERMS.md](TERMS.md) —— **AI 生成结果仅供参考，请自行核对**
- 翻译引擎 [finesub](https://github.com/caca2331/finesub)（GPL-3.0）由用户自行安装，**本软件不打包、不分发**

## 💬 联系作者

反馈问题、提建议、定制需求都欢迎：

<p>
  <b>微信：Star0xcc</b>　
  <img src="web/zs.png" alt="赞赏码" width="120" align="right">
</p>

<p align="center"><sub>如果这个工具帮到了你，欢迎请作者喝杯咖啡 ☕</sub></p>
