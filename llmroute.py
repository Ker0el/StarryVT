#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「翻译走哪个大模型」从引擎的默认配置里接管过来

**为什么必须自己接管**：引擎 finesub 打包的路由表里**只有 Gemini**——
`llm/routing/model_routes.toml` 的 `correction-capable` 组就是
`gemini-free-3_7/3_8/3_6/3_5-flash + gemini-paid-3_7/3_8` 六个模型，而默认预设把
「纠错翻译」这一步绑死在它上面。国内直连 Google 必超时，所以**没有代理的用户
永远翻译不出中文字幕**（客户 eromaster 的 8 次失败都是这一条）。
我们界面上把 DeepSeek 标成「翻译必需」，可引擎那边根本没有 DeepSeek 这条路，
key 填了也是白填——`FINESUB_KEY_DEEPSEEK` 只是躺在 env 文件里没人读。

**引擎留的口子**（读 vendored 源码得来，finesub/paths.py）：
    FINESUB_MODEL_CATALOG → model_catalog.psv  声明"哪家的哪个模型"（事实）
    FINESUB_CONFIG_FILE   → config.toml        声明"哪一步用哪个模型"（编排）
    [llm.model_groups] / [llm.presets] 在同名时**用户文件覆盖打包声明**
自定义 provider 只认 `openai_compat` / `anthropic` 两种 kind，key 走
`FINESUB_KEY_<provider_tier>`（= FINESUB_KEY_DEEPSEEK），和 pipeline 写的 env 文件同路。

两个文件都写在我们自己的配置目录（%APPDATA%\\StarryVT），**不碰引擎的数据目录**：
那里可能放着用户自己写的配置，而且设了 FINESUB_HOME 换盘时整棵树会跟着搬走。

DeepSeek 的 OpenAI 兼容接口在国内直连可用、不需要代理；模型 id 取自
https://api.deepseek.com/models 的实际返回（2026-09-27 实测，两个 id 都对得上）。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "StarryVT"
CATALOG = APP_DIR / "finesub.model_catalog.psv"
CONFIG = APP_DIR / "finesub.config.toml"

# 模型组 / 预设都叫这个名字，日志里一眼能认出是我们的（引擎会把同 id 的覆盖
# 打成一条 warning：`模型组 starryvt / 预设 starryvt`，属正常现象）
GROUP = "starryvt"

# 目录清单的格式：第 1 行表头声明列，之后每行一个"事实"。列名是引擎自己的，
# 一个都不能少、顺序不能换（finesub/llm/routing/model_catalog.py）。
_CATALOG_HEADER = (
    "fact_id|provider_tier|provider_kind|base_url|key_env|display_name|api_model_id|"
    "max_input_tokens|max_output_tokens|context_window|supports_audio|supports_video|"
    "supports_native_search|thinking|token_scale|rpm|tpm|rpd|tpd|is_free|quality_score|"
    "video_high_resolution_only|hint_output_ceiling|fallback_model|quota_pool"
)

# ⚠️ 这两个数（max_input / max_output / context_window）**必须照官方文档写**，
#    写小了不报错、但会**白白慢一倍**：
#    2026-10-17 发现原来写的是 128000|32768（输入差 8 倍、输出差 12 倍），引擎按这个
#    虚假上限规划，打出一句 warning ——
#        「最大输入 128000 < 192000、最大输出 32768 < 64000（窗口会被切得更小，
#          纠错质量与合并判断都会受影响）；规划包络由 ds-flash 决定
#          (95232/32768 tokens，基线 194000/32768)，**窗口数约 ×2.0**」
#    而「纠错翻译」实测占整条流水线约 90% 的耗时，它是**按窗口一个个顺序调**的 ——
#    窗口翻倍 ≈ 那 90% 翻倍。官方文档（api-docs.deepseek.com/zh-cn/quick_start/pricing/）：
#        上下文长度 1M ｜ 输出长度 最大 384K
#    总 token 量不变，所以**只是把调用次数变少，不增加花费**。
#    以后模型换代先回那张表核对一遍，别沿用旧数。
_DEEPSEEK_ROWS = (
    "ds-flash|deepseek|openai_compat|https://api.deepseek.com|FINESUB_KEY_DEEPSEEK|"
    "DeepSeek Flash|deepseek-flash|1000000|384000|1000000|false|false|false|low,low,low|1.0|"
    "-1|-1|-1|-1|false|75|false|false||",
    "ds-pro|deepseek|openai_compat|https://api.deepseek.com|FINESUB_KEY_DEEPSEEK|"
    "DeepSeek V4 Pro|deepseek-v4-pro|1000000|384000|1000000|false|false|false|low,low,low|1.0|"
    "-1|-1|-1|-1|false|80|false|false||",
)

# Gemini 免费池（引擎打包声明里那四个 free 档）。只有配了代理才排进候选链：
# 没代理时它是一条**必然超时**的尾巴，DeepSeek 偶发失败就会去撞六次 10060。
_GEMINI_TAIL = ("gemini-free-3_7-flash", "gemini-free-3_8-flash",
                "gemini-free-3_6-flash", "gemini-free-3_5-flash")


def _on(params: dict, name: str) -> bool:
    """设置项既可能是字符串（key/代理）也可能是布尔（开关），统一判"有没有" """
    v = params.get(name)
    return bool(v.strip()) if isinstance(v, str) else bool(v)


def targets(params: dict) -> list[str]:
    """本次要用的模型链（顺序即优先级，前面的失败才轮到后面的）"""
    deepseek, gemini = _on(params, "deepseek_key"), _on(params, "gemini_key")
    proxy = _on(params, "proxy")
    chain: list[str] = []
    if deepseek:
        chain += ["ds-flash", "ds-pro"]
    if gemini and (proxy or not deepseek):
        chain += list(_GEMINI_TAIL)
    return chain


def wants_gemini(params: dict) -> bool:
    """候选链里有没有 Gemini

    pipeline 写 env 文件时要不要把 `GEMINI_FREE` 交给引擎，跟这个保持一致——
    链里有 Gemini 却不给 key，那一档就永远是"不可用"，白挂。
    """
    return any(t.startswith("gemini-") for t in targets(params))


def _bindings(params: dict) -> list[str]:
    """把哪些任务格子绑到我们自己的模型链上

    `correction-text` 是翻译本体；`research` / `knowledge` 只有不开「联网查证」时才绑——
    `--llm-retrieval native` 会把候选组按"自带联网搜索"过滤，我们这条链没有这种模型，
    绑上去会让调用找不到候选直接失败。留给引擎默认的 Gemini 链（它的 paid 档支持原生搜索）。
    （finesub 0.6 起任务格子只剩 4 个：correction-mm / correction-text / research / knowledge；
    planning-text、search_judge 已不存在，写进去会直接报 ModelRouteConfigError。）
    """
    cells = ["correction-text/quality"]
    if not _on(params, "web_search"):
        cells += ["research/quality", "knowledge/quality"]
    return cells


def render(params: dict) -> tuple[str, str]:
    """生成两个文件的内容；没有可用的翻译模型时返回空串对"""
    chain = targets(params)
    if not chain:
        return "", ""
    cat = "\n".join([_CATALOG_HEADER, *_DEEPSEEK_ROWS]) + "\n"
    quoted = ", ".join(f'"{c}"' for c in chain)
    lines = [
        "# 本文件由「星译」自动生成，每次开工都会重写，请不要手改。",
        "# 作用：告诉翻译引擎 finesub「用哪个大模型做纠错翻译」。",
        "# 引擎自带的默认路由只有 Gemini（国内直连不可用），这里换成 DeepSeek。",
        "",
        "[llm]",
        f'preset = "{GROUP}"',
        "",
        f"[llm.model_groups.{GROUP}]",
        f"targets = [{quoted}]",
        "",
        f"[llm.presets.{GROUP}]",
        'name = "星译"',
        "",
        f"[llm.presets.{GROUP}.bindings]",
    ]
    lines += [f'"{cell}" = "{GROUP}"' for cell in _bindings(params)]
    out = "\n".join(lines) + "\n"
    # 自带一道保险：生成的 TOML 自己先解析一遍。写坏一个字符就会让**每一次翻译**
    # 都卡在 "Invalid FineSub config TOML"，而引擎的报错完全看不出是我们写的文件。
    tomllib.loads(out)
    return cat, out


def _write_if_changed(path: Path, text: str) -> None:
    """内容没变就别碰文件（引擎按 mtime 判断要不要重读配置）"""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")


def env(params: dict) -> dict:
    """写文件并返回要注入引擎的环境变量；用不上时返回空 dict（引擎走它自己的默认）"""
    cat, cfg = render(params)
    if not cat:
        return {}
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _write_if_changed(CATALOG, cat)
    _write_if_changed(CONFIG, cfg)
    return {"FINESUB_MODEL_CATALOG": str(CATALOG), "FINESUB_CONFIG_FILE": str(CONFIG)}


def describe(params: dict) -> list[str]:
    """诊断报告用：翻译实际会走哪家、配置文件在不在"""
    chain = targets(params)
    lines = ["----- 翻译模型路由 -----"]
    if not chain:
        lines.append("没有可用的翻译模型：DeepSeek / Gemini 两个 Key 都没填")
        return lines
    who = "DeepSeek" if chain[0].startswith("ds-") else "Gemini"
    lines.append(f"翻译走: {who}（候选链 {' → '.join(chain)}）")
    for p in (CATALOG, CONFIG):
        lines.append(f"{p.name}: {'已生成' if p.is_file() else '未生成'}（{p}）")
    lines.append("(引擎打包的默认路由只有 Gemini，国内直连不可用；这里是我们覆盖后的结果)")
    return lines


def status(params: dict) -> tuple[str, str]:
    """环境自检用：(状态, 一句人话)。状态取值同 doctor 的 ok/warn/bad。"""
    chain = targets(params)
    if not chain:
        return "bad", ("没填 DeepSeek Key——翻译用它，必填。"
                       "到「设置」里填上就能翻译（翻译本地文件不需要代理）。")
    if chain[0].startswith("ds-"):
        return "ok", "翻译走 DeepSeek（国内直连，不需要代理）"
    if _on(params, "proxy"):
        return "ok", "翻译走 Gemini（已配代理）"
    return "warn", ("只填了 Gemini Key、又没配代理：国内直连 Google 一定会超时，翻译出不来。"
                    "把「设置」里的 DeepSeek Key 填上就不用代理了。")
