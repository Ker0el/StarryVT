#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字幕字体：随包字体的文件位置。

「思源黑体 Light」不依赖用户自己装字体，随包带了一份从 Noto Sans SC 可变字体
抽出来的 Light（wght=300）静态实例：

- 字体内部族名改成了 `Starry Han Sans Light`。OFL 1.1 第 3 条要求修改版不得使用
  Reserved Font Name（这一份的保留名是 `Source`），所以不能继续叫 Noto/Source。
- 界面里显示的名字仍然是「思源黑体 Light」，用户不需要知道内部族名。
- 许可证原文在 `assets/fonts/OFL.txt`，随安装包一起分发。
"""
from __future__ import annotations

import tools

# 显示用的逻辑字体名 -> 随包文件（相对 assets/）
BUNDLED = {
    "Starry Han Sans Light": "fonts/StarryHanSans-Light.ttf",
}


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
