#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设置存取：%APPDATA%\\StarryVT\\settings.json（含 MiMo key 的回退读取链）"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT"
SETTINGS_PATH = APP_DIR / "settings.json"

DEFAULTS: dict = {
    "source": "",                        # 本次输入的 视频文件路径 / 视频链接 / 文件夹 / 合集链接
    "batch_kind": "folder",              # 批量模式来源：folder=文件夹 / playlist=YouTube 合集
    "mimo_key": "",                      # MiMo（配音）—— 用户自己申请
    "deepseek_key": "",                  # DeepSeek（翻译，必需）—— 用户自己申请
    "gemini_key": "",                    # Gemini（翻译增强，可选）—— 用户自己申请
    "voice": "冰糖",
    "source_lang": "en",
    "extra_info": "",                    # 给翻译模型的一句话背景（可选）
    "yt_subs": "manual",                 # YouTube 字幕：off=不拉 / manual=只人工轨 / auto=没人工轨退自动轨
    "yt_auto_extra": True,               # 粘上链接后按标题自动填「背景信息」
    "finesub_exe": "finesub",            # 空/PATH 名 = PATH 查找
    "finesub_home": "",                  # 空 = 用引擎自己的默认数据目录（推荐）
    "finesub_bad_homes": [],             # 建过链接失败的目录（os error 448），自动记录、不再挑它
    "hf_endpoint": "https://hf-mirror.com",
    "github_mirror": "auto",             # GitHub 加速镜像：auto=自动挑 / off=不用 / 或填镜像地址
    "github_mirror_ok": "",              # 上次探测到的可用镜像（缓存，见 mirrors.py）
    "github_mirror_at": 0,               # 上次探测的时间戳（几小时内不再重探）
    # ⚠️ 默认必须留空。原来这里预填了 http://127.0.0.1:7897，而「填了代理」被当成
    #    「这人能上外网」的凭据（见 mirrors.enabled），于是没装代理软件的机器上
    #    镜像和清华源全被跳过、所有请求撞死端口（WinError 10061），一键初始化必然失败。
    #    设置页的输入框有 PlaceholderText 提示，想填的人照着填就行，用不着预填。
    #    ⚠️ 注意这只救新用户：save() 会把整份配置写回文件，老用户机器上已经写死了旧值。
    #       老用户靠 mirrors.proxy_ok 的探测兜（探不通就自动回退到镜像）。
    "proxy": "",                         # 本机代理（Clash 等）；留空=直连
    "output_dir": "",                    # 空 = 视频旁边的 output\（链接下载用软件目录）
    # 用户 2026-10-15：翻完直接打开成片，**默认开**
    # ⚠️ 只对新装机有效 —— save() 会把整份配置写回文件，老用户文件里已经存着 false。
    "auto_open": True,                   # 单个视频翻译完自动用默认播放器打开成片（批量不打开）
    "bg_opacity": 0.55,                  # 界面星空背景的亮度（0 = 纯深色底）
    "default_output": "final",           # 工作页「输出」的默认值：final=出成片 / dub=只配音 / subs=只出字幕 / download=只下载视频
    "do_dub": True,
    "burn_subs": True,
    "download_only": False,              # True = 只下载/输出视频，跳过翻译、配音和压制（由 default_output=download 触发）
    "orig_vol": 0.15,                    # 成片里保留的原声音量（0 = 纯中文，1 = 与原声等响）
    "tts_style": "",                     # 配音「怎么读」的风格指令；空 = 用 dubber.DEFAULT_STYLE
    "tts_speed": "auto",                 # 配音整体语速：auto = 标准语速，只按每句时长自动适配 / 5 = 整体 +5% / 10 = 整体 +10%
    "subtitle_font": "Starry Han Sans Light",  # 烧录字幕字体（随包的思源黑体 Light，见 subtitle_fonts.py）
    "subtitle_font_file": "",            # 已停用：字体固定为随包的思源黑体 Light
    "subtitle_size": 42,                 # 字号基准；太长的行会按画面宽度自动缩小
    "subtitle_spacing": 0,               # 字间距（px，负数更紧）
    "subtitle_color": "#FFFFFF",         # 字幕颜色（#RRGGBB）
    "subtitle_outline": 2.2,             # 描边粗细
    "subtitle_margin_v": 30,             # 距画面底部的距离
    "auto_subtitle": False,              # True = 视频旁边有同名 .srt/.vtt 时直接拿来翻译，跳过语音识别
    "subtitle_review": False,            # True = 单个视频翻译完成后暂停，双语对照检查译文，改完继续配音/压制
    "tts_clone": False,                  # True = 用原视频里的人声复刻音色来配音（见 clone_engine）
    "clone_engine": "local",             # 音色克隆用哪个引擎：local = 本机 IndexTTS-2.5（默认，免费、不联网，
                                         # 速度快；没装模型时自动退回云端，见 localclone.py）/
                                         # cloud = MiMo 云端（限时免费，但受网络和限流影响，可能忽快忽慢）
    "video_context": False,              # True = 允许把媒体上传给 Gemini 做视频语境纠错（需要可用的 Gemini key）
    "web_search": False,                 # True = 联网查证（走 Google 搜索，需代理且较慢）
    "deepseek_web_search": False,        # True = DeepSeek 联网查证（翻译前用 DeepSeek 搜索补充背景，不需要代理）
    # 更新源：**留空 = GitHub Releases**（用户 2026-10-17 决定不再用国内对象存储）。
    # 填了别的值就以填的为准（方便本地造假清单调试）。
    "update_url": "",
    "auto_update_check": True,           # 启动时自动检查更新
    # 翻译时自动学习术语并写回词条库（引擎 `--knowledge update`；关掉 = 只读不写）。
    # 「越用越准」的来源之一，见 builtin_kb.py。会多花一轮 LLM 调用，所以留了开关。
    "learn_terms": True,
    "auto_update_downloader": True,      # 启动时自动更新随包的下载器（yt-dlp，见 ytdlp.py）
    "ytdlp_channel": "",                 # 下载器通道：空/stable = 官方稳定版；nightly = 每日构建（反爬自救后自动写入）
    "ytdlp_version": "",                 # 已装下载器版本（更新后写入，界面显示用）
}

# MiMo TTS 合法音色（2026-09-14 逐个调用 API 验证；API 返回的可用列表）
VOICES = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean", "mimo_default"]
LANGS = [("en", "英语"), ("ja", "日语"), ("ko", "韩语"), ("ru", "俄语"),
         ("zh", "中文"), ("zh-en", "中文 → 英语")]

# 各服务的申请入口（设置页展示给用户）
APPLY_LINKS = {
    "mimo": "https://platform.xiaomimimo.com/console/api-keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "gemini": "https://aistudio.google.com/apikey",
}


def load() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass  # 配置损坏时退回默认值
    # 2026-10-17 用户定：字幕字体只保留随包的思源黑体 Light，老配置里的其它字体迁移过来
    data["subtitle_font"] = "Starry Han Sans Light"
    data["subtitle_font_file"] = ""
    return data


def save(patch: dict) -> dict:
    data = load()
    data.update({k: v for k, v in patch.items() if k in DEFAULTS or k in ("mimo_key", "deepseek_key", "gemini_key")})
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def resolve_mimo_key(cfg: dict | None = None) -> str:
    """MiMo key：设置 > 环境变量（不内置任何默认值，用户自行申请）"""
    cfg = cfg or load()
    return cfg.get("mimo_key") or os.environ.get("MIMO_API_KEY", "")


def mask_key(key: str) -> str:
    """给前端展示用：只留尾 4 位"""
    return f"...{key[-4:]}" if len(key) > 8 else ("已配置" if key else "")
