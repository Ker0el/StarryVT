#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕字体：随包字体的位置 + 用户自定义字体文件的族名解析。

「思源黑体 Light」不依赖用户自己装字体，随包带了一份从 Noto Sans SC 可变字体
抽出来的 Light（wght=300）静态实例：

- 字体内部族名改成了 `Starry Han Sans Light`。OFL 1.1 第 3 条要求修改版不得使用
  Reserved Font Name（这一份的保留名是 `Source`），所以不能继续叫 Noto/Source。
- 界面里显示的名字仍然是「思源黑体 Light」，用户不需要知道内部族名。
- 许可证原文在 `assets/fonts/OFL.txt`，随安装包一起分发。

★ 2026-10-21 起支持用户自定义字体文件（设置 → 字幕样式 → 字体）。
  libass 按 **字体族名** 找字形，所以不能把「文件名」直接当 Fontname 写进 ASS ——
  必须先把 .ttf/.otf/.ttc 里的 name 表读出来，拿到真正的族名。
"""
from __future__ import annotations

import struct
from pathlib import Path

import tools

# 显示用的逻辑字体名 -> 随包文件（相对 assets/）
BUNDLED = {
    "Starry Han Sans Light": "fonts/StarryHanSans-Light.ttf",
}

DEFAULT_FONT = "Starry Han Sans Light"

#: 族名缓存：{绝对路径: ((mtime_ns, size), 族名)}
_NAME_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _sfnt_offset(data: bytes) -> int:
    """TTF/OTF 返回 0；TTC 字体集合返回第一份字体的偏移。"""
    if data[:4] == b"ttcf" and len(data) >= 16:
        try:
            num = struct.unpack(">I", data[8:12])[0]
            if num >= 1:
                return int(struct.unpack(">I", data[12:16])[0])
        except struct.error:
            pass
    return 0


def _family_from_bytes(data: bytes) -> str:
    """从字体二进制里读 name 表，返回族名（读不出来返回空串）。"""
    base = _sfnt_offset(data)
    if len(data) < base + 12:
        return ""
    try:
        num_tables = struct.unpack(">H", data[base + 4:base + 6])[0]
    except struct.error:
        return ""
    name_off = name_len = 0
    for i in range(min(num_tables, 512)):
        rec = base + 12 + i * 16
        if rec + 16 > len(data):
            break
        if data[rec:rec + 4] == b"name":
            name_off, name_len = struct.unpack(">II", data[rec + 8:rec + 16])
            break
    if not name_off or name_off + 6 > len(data):
        return ""
    try:
        count, str_off = struct.unpack(">HH", data[name_off + 2:name_off + 6])
    except struct.error:
        return ""
    best: tuple[tuple[int, int, int], str] = ((0, 0, 0), "")
    for i in range(min(count, 512)):
        rec = name_off + 6 + i * 12
        if rec + 12 > len(data):
            break
        try:
            pid, eid, lid, nid, ln, off = struct.unpack(">HHHHHH", data[rec:rec + 12])
        except struct.error:
            continue
        if nid not in (1, 16):          # 1 = family，16 = typographic family
            continue
        start = name_off + str_off + off
        raw = data[start:start + ln]
        if pid == 3:                    # Windows：UTF-16BE
            s = raw.decode("utf-16-be", "replace").strip()
        elif pid == 1:                  # Mac Roman
            s = raw.decode("latin-1", "replace").strip()
        else:
            continue
        if not s:
            continue
        # 优先 typographic family（16）> family（1）；优先英文（0x0409）；优先 Windows 记录
        score = (2 if nid == 16 else 1, 2 if lid == 0x0409 else 1, 1 if pid == 3 else 0)
        if score > best[0]:
            best = (score, s)
    return best[1]


def family_of(path: str | Path) -> str:
    """读字体文件的族名（带 mtime/size 缓存，改过文件会自动重读）。"""
    p = Path(str(path))
    try:
        st = p.stat()
    except OSError:
        return ""
    key = str(p.resolve()).lower()
    hit = _NAME_CACHE.get(key)
    sig = (int(st.st_mtime_ns), int(st.st_size))
    if hit and hit[0] == sig:
        return hit[1]
    try:
        fam = _family_from_bytes(p.read_bytes())
    except Exception:  # noqa: BLE001 - 坏文件/权限不足都只是"读不出族名"
        fam = ""
    _NAME_CACHE[key] = (sig, fam)
    return fam


def file_for(font: str, explicit: str = "") -> str:
    """返回要交给 libass 的字体文件路径；没有随包文件就返回空串。

    用户自己填了字体文件（subtitle_font_file）时优先用用户的。
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    rel = BUNDLED.get((font or "").strip())
    if not rel:
        return ""
    path = tools.bundle_dir() / "assets" / rel
    return str(path) if path.is_file() else ""


def resolve(font: str, explicit: str = "") -> tuple[str, str]:
    """返回 (ASS 里要写的字体族名, 字体文件路径)。

    用户选的字体文件还在 → 族名从文件里读（读不出来就用文件名）。
    文件被删/路径失效 → 退回随包的思源黑体，别让成片变成系统默认字体。
    """
    p = (explicit or "").strip()
    if p and Path(p).is_file():
        return (family_of(p) or Path(p).stem), p

    bundled = file_for(DEFAULT_FONT, "")
    if bundled:
        return DEFAULT_FONT, bundled
    # 极端情况：随包字体被杀了，至少尊重用户填的名字（系统字体也许能用）
    return ((font or "").strip() or DEFAULT_FONT), ""
