"""把 说明.txt 转成 说明.rtf —— 让里面的 B 站链接在安装向导里真能点。

为什么不能直接给 Inno 喂 .txt：InfoBeforeFile 走纯文本时 URL 只是几个字，
点不动。RTF 里用 HYPERLINK 域才会变成可点的链接（Inno 的说明页用的是支持
EN_LINK 的 rich edit 控件）。

所以**改说明只改 说明.txt**，然后跑这个脚本重新生成 .rtf，别直接编辑 .rtf。

坑：
  · RTF 里不能直接放中文 —— 非 ASCII 一律写成 \\u<十进制>? 转义
  · 反斜杠和花括号本身也要转义
  · 码位超过 0x7FFF 的要减 65536（RTF 用有符号 16 位）
  · 字体表里只写 ASCII 字体名

用法：
    python mkrtf.py
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

SRC = r'说明.txt'
DST = r'说明.rtf'
ISSPATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'installer.iss')

LINK_RE = re.compile(r'https?://[^\s)]+')


def installer_version():
    """从 installer.iss 读 AppVersion —— 说明页第一行别再手写版本号（会忘）"""
    try:
        for line in io.open(ISSPATH, encoding='utf-8'):
            m = re.match(r'\s*#define\s+AppVersion\s+"([^"]+)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ''


def esc(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '{':
            out.append('\\{')
        elif ch == '}':
            out.append('\\}')
        elif o < 128:
            out.append(ch)
        elif o > 0xFFFF:                       # 基本平面外：拆代理对
            o -= 0x10000
            hi = 0xD800 + (o >> 10)
            lo = 0xDC00 + (o & 0x3FF)
            out.append('\\u%d?\\u%d?' % (hi - 65536, lo - 65536))
        else:
            out.append('\\u%d?' % (o - 65536 if o > 32767 else o))
    return ''.join(out)


def link(url, text):
    return ('{\\field{\\*\\fldinst{HYPERLINK "%s"}}'
            '{\\fldrslt{\\ul\\cf2 %s}}}' % (url, esc(text)))


def linkify(line):
    """一行里所有 URL 都做成可点的链接（GitHub / 文档 / B 站主页）"""
    out, last = [], 0
    for m in LINK_RE.finditer(line):
        out.append(esc(line[last:m.start()]))
        out.append(link(m.group(0), m.group(0)))
        last = m.end()
    out.append(esc(line[last:]))
    return ''.join(out)


def main():
    lines = io.open(SRC, encoding='utf-8').read().split('\n')
    ver = installer_version()
    if ver and lines and lines[0].lstrip('﻿').startswith('星译'):
        lines[0] = '星译 ' + ver
    body = []
    for ln in lines:
        s = ln.rstrip('\r')
        body.append(linkify(s))

    rtf = ('{\\rtf1\\ansi\\ansicpg936\\deff0\\uc1\n'
           '{\\fonttbl{\\f0\\fnil\\fcharset134 Microsoft YaHei;}}\n'
           '{\\colortbl;\\red0\\green0\\blue0;\\red0\\green90\\blue200;}\n'
           '\\viewkind4\\pard\\f0\\fs18\\cf1\n'
           + '\\par\n'.join(body) + '\\par\n}\n')

    # Inno 按内容认 RTF，但为保险写 ASCII —— 文件里不该有裸的非 ASCII 字节
    io.open(DST, 'w', encoding='ascii', newline='\n').write(rtf)

    bad = sum(1 for c in rtf if ord(c) > 127)
    print('%s -> %s  (%d 字节)' % (SRC, DST, len(rtf)))
    print('  非 ASCII 残留 %d 处（必须 0）' % bad)
    print('  可点链接 %d 个' % rtf.count('HYPERLINK'))
    return 1 if (bad or rtf.count('HYPERLINK') == 0) else 0


if __name__ == '__main__':
    sys.exit(main())
