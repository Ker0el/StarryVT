#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设置存取：%APPDATA%\\StarryVT\\settings.json（含 MiMo key 的回退读取链）"""
from __future__ import annotations

import json
import os
from pathlib import Path

import tools

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT"
SETTINGS_PATH = APP_DIR / "settings.json"

#: 这几项是磁盘路径，读写时都换成真实长路径（8.3 短名见 tools.long_path）。
#: 只列**程序自己算出来写进去**的两个；用户手填的 output_dir 不在此列，
#: 免得把人家故意写的相对路径 / 特殊写法改掉。
PATH_KEYS = ("finesub_home", "data_home")
_healed = False          # 这个进程里是否已经自愈过一次（见 load）

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
    # 星译自己的数据根目录：语音克隆、配音缓存、缩略图、wheel 缓存都放这里。
    # 空 = %LOCALAPPDATA%\StarryVT（C 盘）。换盘见 paths.py / 设置 → 输出 → 数据目录。
    "data_home": "",
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
    # True = 输出目录名和成片文件名用**翻译后的**名字（原文件不改）。
    # 封面之外，文件名/文件夹名也常常是外语；每个视频多一次很短的 DeepSeek 请求。
    "translate_names": True,
    # 用户 2026-10-15：翻完直接打开成片，**默认开**
    # ⚠️ 只对新装机有效 —— save() 会把整份配置写回文件，老用户文件里已经存着 false。
    "auto_open": True,                   # 单个视频翻译完自动用默认播放器打开成片（批量不打开）
    "bg_opacity": 0.55,                  # 界面星空背景的亮度（0 = 纯深色底）
    "default_output": "final",           # 工作页「输出」的默认值：final=出成片 / dub=只配音 / subs=只出字幕 / download=只下载视频
    "do_dub": True,
    "burn_subs": True,
    "download_only": False,              # True = 只下载/输出视频，跳过翻译、配音和压制（由 default_output=download 触发）
    "asr_only": False,                   # True = 只提取原文字幕（ASR），不调 DeepSeek、不配音、不压制
    "orig_vol": 0.0,                     # 成片里保留的原声音量（0 = 纯中文，1 = 与原声等响；2026-10-28 默认改为 0%）
    "tts_style": "",                     # 配音「怎么读」的风格指令；空 = 用 dubber.DEFAULT_STYLE
    "tts_speed": "auto",                 # 配音整体语速：auto = 标准语速，只按每句时长自动适配 / 5 = 整体 +5% / 10 = 整体 +10%
    "subtitle_font": "Starry Han Sans Light",  # 烧录字幕字体（随包的思源黑体 Light，见 subtitle_fonts.py）
    "subtitle_font_file": "",            # 已停用：字体固定为随包的思源黑体 Light
    "subtitle_size": 42,                 # 字号基准；太长的行会按画面宽度自动缩小
    "subtitle_spacing": 0,               # 字间距（px，负数更紧）
    "subtitle_color": "#FFFFFF",         # 字幕颜色（#RRGGBB）
    "subtitle_outline": 2.2,             # 描边粗细
    "subtitle_outline_color": "#000000", # 描边颜色；默认黑色（2026-10-28 用户定，界面不再有「自动」）
    "subtitle_margin_v": 30,             # 距画面底部的距离
    # 双语字幕（用户 2026-10-21 要求）：原文 + 中文两行一起烧进视频。
    # 只影响**烧录进成片**的字幕；输出目录里那份 .srt 仍然是纯中文（配音要读它）。
    "subtitle_bilingual": False,
    "subtitle_bilingual_order": "src_top",   # src_top = 原文在上；src_bottom = 原文在下
    "subtitle_bilingual_scale": 80,          # 原文行字号 = 中文字号的百分之多少
    "subtitle_review": False,            # True = 单个视频翻译完成后暂停，双语对照检查译文，改完继续配音/压制
    # 翻译模式：standard = 人声分离 + AI 纠错翻译 + MiMo/本机克隆配音；
    # fast = 快速模式：跳过人声分离，Google 机翻 + 微软 edge-tts（不花 API 费用）
    "mode": "standard",
    "edge_voice": "zh-CN-XiaoxiaoNeural",  # 快速模式用的微软音色（见 dubber.EDGE_VOICES）
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
    "learn_terms": False,
    "auto_update_downloader": True,      # 启动时自动更新随包的下载器（yt-dlp，见 ytdlp.py）
    "ytdlp_channel": "",                 # 下载器通道：空/stable = 官方稳定版；nightly = 每日构建（反爬自救后自动写入）
    "ytdlp_version": "",                 # 已装下载器版本（更新后写入，界面显示用）
}

# MiMo TTS 合法音色（2026-09-14 逐个调用 API 验证；API 返回的可用列表）
VOICES = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean", "mimo_default"]
LANGS = [("en", "英语"), ("ja", "日语"), ("ko", "韩语"), ("ru", "俄语"),
         ("de", "德语"), ("zh", "中文"), ("zh-en", "中文 → 英语")]

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
    # 2026-10-21：字体可以自定义了，不再强制写死。
    # 但老配置里可能还留着早期版本的系统字体名（那时候没有 subtitle_font_file），
    # 统一归到随包字体；真正选过字体文件的（有 font_file）原样保留。
    if not (data.get("subtitle_font_file") or "").strip():
        data["subtitle_font"] = "Starry Han Sans Light"
    # 8.3 短路径（D:\PROGRA~1\…）在设置里一旦钉住就永远回不来 —— 重启、重装都不改。
    # 读出来统一换成长路径；第一次命中时顺手把文件也改掉（只做一次，load 是高频调用）。
    global _healed
    changed = False
    for key in PATH_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            fixed = str(tools.long_path(val))
            if fixed != val:
                data[key] = fixed
                changed = True
    # 2026-10-26 客户报障：把「翻译引擎路径」填成了程序目录（`D:\StarryVT`）。
    # v6.24 起解析时会跳过它（先找目录里的 finesub.exe，没有就自动找），但设置页
    # 一直显示着这个错值、诊断报告也一直标黄，而**修好引擎后它也不会自己回正**。
    # 无用的值直接清掉（留空 = 自动查找），只在第一次命中时写回文件。
    exe_val = data.get("finesub_exe")
    if isinstance(exe_val, str) and exe_val.strip():
        raw = exe_val.strip().strip('"').strip()
        if raw and (os.path.isabs(raw) or "\\" in raw or "/" in raw):
            try:
                p = Path(raw)
                if p.is_dir() and not (p / "finesub.exe").is_file():
                    data["finesub_exe"] = ""
                    changed = True
                    try:
                        import diag
                        diag.write(f"[settings] 翻译引擎路径填成了目录（{raw}）"
                                   f"，已改为自动查找", "run")
                    except Exception:  # noqa: BLE001
                        pass
            except OSError:
                pass
    if changed and not _healed:
        _healed = True
        try:
            save({**{k: data[k] for k in PATH_KEYS},
                  "finesub_exe": data.get("finesub_exe", "")})
        except Exception:  # noqa: BLE001 - 写不进去也要把长路径给调用方
            pass
    return data


def save(patch: dict) -> dict:
    data = load()
    data.update({k: v for k, v in patch.items() if k in DEFAULTS or k in ("mimo_key", "deepseek_key", "gemini_key")})
    # 落盘前再归一化一次：以后写进来的也永远是长路径
    for key in PATH_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            data[key] = str(tools.long_path(val))
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # ★ 原子写：先写同目录临时文件，再 os.replace 换过去。
    #   以前直接 write_text，装引擎/换数据目录时正好断电或被杀进程，settings.json
    #   会只剩半截 JSON；load() 读坏就回退默认值，用户的 API Key 和所有设置一起没了。
    #   （2026-10-19 全面检查时补的；同目录替换在 Windows 上也是原子的。）
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    for i in range(3):
        try:
            os.replace(tmp, SETTINGS_PATH)
            return data
        except OSError:
            if i == 2:
                break
            import time
            time.sleep(0.05)
    # 极少数情况下目标正被别的进程短暂打开，replace 会让步；退回直写，至少这次能存上
    SETTINGS_PATH.write_text(text, encoding="utf-8")
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return data


def resolve_mimo_key(cfg: dict | None = None) -> str:
    """MiMo key：设置 > 环境变量（不内置任何默认值，用户自行申请）"""
    cfg = cfg or load()
    return cfg.get("mimo_key") or os.environ.get("MIMO_API_KEY", "")


def mask_key(key: str) -> str:
    """给前端展示用：只留尾 4 位"""
    return f"...{key[-4:]}" if len(key) > 8 else ("已配置" if key else "")
