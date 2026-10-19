#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按视频标题认软件，自动填「背景信息」那一栏。

填进去的那句话有两个用处：
  1. 直接进纠错翻译的提示词，模型先知道自己在看什么教程；
  2. 里面点名的软件，正好和知识库里的术语条目对上号——模型看索引时会挑中那一条，
     术语行（正确拼写 + 官方中文定名 + 常见误听）就跟着进了提示词。

标题从哪来：链接走 yt-dlp 取一次标题（不下载视频，一两秒）；本地文件直接用文件名。
"""
from __future__ import annotations

import re

#: (中文名, 标准写法, 关键词) —— 关键词按小写匹配，长的排前面免得被短的抢走
#: 关键词只在**词边界**上匹配，所以 "ue5" 不会命中 "value"，"sp" 这种两字母的
#: 缩写一律不要单列（会到处误伤），要写就写全。
TABLE: list[tuple[str, str, tuple[str, ...]]] = [
    ("虚幻引擎", "Unreal Engine",
     ("unreal engine", "unreal", "ue5", "ue4", "ue 5", "ue 4", "blueprint", "niagara", "lumen")),
    ("Unity", "Unity", ("unity", "unity3d", "unity 3d", "urp", "hdrp")),
    ("Godot", "Godot", ("godot", "gdscript")),
    ("Substance 3D Designer", "Substance 3D Designer",
     ("substance designer", "substance 3d designer", "sbsar", "substance graph")),
    ("Substance 3D Painter", "Substance 3D Painter",
     ("substance painter", "substance 3d painter", "substance")),
    ("Blender", "Blender", ("blender", "geometry nodes", "eevee")),
    ("Houdini", "Houdini", ("houdini", "sidefx", "vellum", "karma", "sop", "vex")),
    ("SpeedTree", "SpeedTree", ("speedtree", "speed tree")),
    ("Cascadeur", "Cascadeur", ("cascadeur",)),
    ("Gaea", "Gaea", ("gaea", "quadspinner")),
    ("EmberGen", "EmberGen", ("embergen", "ember gen", "jangafx")),
    ("LiquiGen", "LiquiGen", ("liquigen", "liquid gen")),
    ("Character Creator", "Character Creator",
     ("character creator", "reallusion", "cc4", "cc3", "headshot", "accurig", "iclone")),
    ("Plasticity", "Plasticity", ("plasticity",)),
    ("Marmoset Toolbag", "Marmoset Toolbag", ("toolbag", "marmoset")),
    ("Maya", "Autodesk Maya", ("maya", "autodesk maya", "arnold")),
    ("3ds Max", "3ds Max", ("3ds max", "3dsmax", "3d max")),
    ("ZBrush", "ZBrush", ("zbrush", "zbrushcore")),
    ("Marvelous Designer", "Marvelous Designer", ("marvelous designer", "marvelous")),
    ("Nuke", "Nuke", ("nuke", "foundry nuke")),
    ("DaVinci Resolve", "DaVinci Resolve", ("davinci resolve", "davinci")),
    ("After Effects", "After Effects", ("after effects", "adobe after effects")),
    ("Photoshop", "Photoshop", ("photoshop",)),
    ("Illustrator", "Illustrator", ("illustrator",)),
    # AI 工具（2026-10-16 加）：知识库里已建了这三条术语条目，这里点名后模型才会挑中它们。
    # 名字要和知识库条目的显示名**逐字一致**（`finesub knowledge show <名>`）。
    ("Claude Code", "Claude Code", ("claude code", "claude", "anthropic")),
    ("GPT", "GPT", ("chatgpt", "gpt", "openai", "gpt4", "gpt5")),
    ("DeepSeek", "DeepSeek", ("deepseek", "deep seek", "深度求索")),
    # 2026-10-17 加：MCP 是协议名，不是软件名；标题里出现时把词条带上。
    ("MCP", "MCP", ("mcp", "model context protocol", "模型上下文协议")),
]

#: 一句话说明「怎么译」，进背景信息
ADVICE = ("术语按该软件的官方中文界面定名；官方保留英文的（Nanite / Lumen / VEX 这类）"
          "照抄英文，不要自己造译名。")


def _hit(title: str, keys: tuple[str, ...]) -> bool:
    low = title.lower()
    for k in keys:
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", low):
            return True
    return False


def guess(title: str) -> list[tuple[str, str]]:
    """从标题里认出讲的哪几款软件（可能不止一个），按表里的顺序返回"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for zh, en, keys in TABLE:
        if zh in seen:
            continue
        if _hit(title, keys):
            out.append((zh, en))
            seen.add(zh)
    return out


def build_extra(title: str, limit: int = 2) -> str:
    """标题 → 背景信息那一栏该填的话

    认不出软件也返回一句——标题本身就是有用的背景，别浪费。
    """
    title = (title or "").strip()
    hits = guess(title)[:limit]
    if not hits:
        return f"视频标题：{title}" if title else ""
    names = "、".join(zh if zh == en else f"{zh}（{en}）" for zh, en in hits)
    head = f"视频标题：{title}。" if title else ""
    return f"{head}这是{names}的教学视频。{ADVICE}"


def guess_many(titles: list[str], limit: int = 2) -> list[tuple[str, str]]:
    """一批标题（合集）→ 命中最多的那几款软件

    合集基本都是一个软件一个系列，所以按「命中多少条」排序，比只看第一条稳。
    """
    tally: dict[str, tuple[int, str, str]] = {}
    for t in titles or []:
        for zh, en in guess(str(t)):
            n, _, _ = tally.get(zh, (0, zh, en))
            tally[zh] = (n + 1, zh, en)
    ranked = sorted(tally.values(), key=lambda row: -row[0])
    return [(zh, en) for _n, zh, en in ranked[:limit]]


def build_extra_many(titles: list[str], limit: int = 2) -> str:
    """一批标题 → 背景信息（合集用）"""
    titles = [str(t).strip() for t in (titles or []) if str(t).strip()]
    if not titles:
        return ""
    hits = guess_many(titles, limit)
    if not hits:
        return f"视频标题：{titles[0]}（共 {len(titles)} 集）" if len(titles) > 1 else f"视频标题：{titles[0]}"
    names = "、".join(zh if zh == en else f"{zh}（{en}）" for zh, en in hits)
    if len(titles) > 1:
        return f"这是一套{names}的教学视频合集（共 {len(titles)} 集，首集：{titles[0]}）。{ADVICE}"
    return f"视频标题：{titles[0]}。这是{names}的教学视频。{ADVICE}"


if __name__ == "__main__":
    import sys

    for t in (sys.argv[1:] or ["Unreal Engine 5 Beginner Tutorial",
                               "Houdini Vellum 布料解算入门",
                               "Top 10 Blender addons 2026"]):
        print(f"{t}\n  → {build_extra(t)}\n")
