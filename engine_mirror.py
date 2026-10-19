#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把引擎清单恢复到 GitHub 原始下载源。

2026-10-17 起用户决定不再维护腾讯云 COS 自建源。老版本安装引擎时会把
`runtime-manifest.json` / `model-manifest.json` 里的 GitHub 地址改写成本地对象存储；
新版本调用 apply() 时会把 `.starryvt-orig` 备份恢复回去，确保引擎从上游 GitHub 下载。

如果以后又重新启用 `mirrors.SELF_HOSTED`，原来的「逐条确认桶里真的有、再改写」逻辑仍然保留。
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import mirrors
import wheels

BACKUP_SUFFIX = ".starryvt-orig"
NAMES = ("runtime-manifest.json", "model-manifest.json")


def manifests() -> list[Path]:
    """引擎里那两张清单（和 pylock 在同一个目录）"""
    out: list[Path] = []
    for d in wheels._bootstrap_dirs():
        for n in NAMES:
            p = d / n
            if p.is_file():
                out.append(p)
    return out


def _reachable(url: str, timeout: float = 6.0) -> bool:
    """那个对象现在真的下得到吗（HEAD，不取正文 —— 610MB 的对象也是几百毫秒）

    ★★ **每一条都要单独确认**，不能只探一个就当全体可用：
    改写是**没有退路**的 —— 清单一旦指向我们的地址，引擎就不会再去 GitHub 了。
    所以只要有哪一条对象没传上去 / 被删了，那条**必须保持原样**（继续走 GitHub，慢但能用），
    否则就变成「必然装不上」。这是本模块唯一的硬要求。
    """
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": mirrors.UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def apply(report=None) -> bool:
    """装完引擎、开始装运行环境之前调一次。

    返回 True = 清单已是 GitHub 原始地址（或该恢复的已恢复）；
    False = 还没找到引擎清单（引擎没装完）。
    """
    files = manifests()
    if not files:
        return False                       # 引擎还没装，等装完再说

    # 2026-10-17：不再用自建源；把老版本改写过的清单恢复成 GitHub 原始地址
    if not mirrors.SELF_HOSTED:
        n = restore()
        if report:
            report("engine", None,
                   f"引擎下载源已恢复为 GitHub（{n} 个清单）" if n
                   else "引擎下载源使用 GitHub 原始地址")
        return True

    # 只挑「清单里真的出现过」的那几条，别为用不上的地址白白探活
    hits: dict[str, str] = {}
    for f in files:
        text = wheels._read(f)
        for url, ours in mirrors.SELF_HOSTED.items():
            if url in text:
                hits[url] = ours
    if not hits:
        if report:
            report("engine", None, "引擎的下载源无需改写（清单里没有我们托管的那几条）")
        return True

    # ★ 逐条确认「桶里真的有」——只改确认拿得到的那些（理由见 _reachable 的说明）
    live: dict[str, str] = {}
    dead: list[str] = []
    for url, ours in hits.items():
        if _reachable(ours):
            live[url] = ours
        else:
            dead.append(url.rsplit("/", 1)[-1])
    if dead and report:
        report("engine", None, "这几样自建源上没有，仍走 GitHub：" + "、".join(dead))
    if not live:
        if report:
            report("engine", None, "自建下载源当前连不上，这一轮仍走 GitHub（慢，但能用）")
        return False
    hits = live

    changed = 0
    for f in files:
        text = wheels._read(f)
        if any(ours in text for ours in hits.values()):
            continue                       # 已经改过（幂等）
        new = text
        for url, ours in hits.items():
            new = new.replace(url, ours)
        if new == text:
            continue
        backup = f.with_name(f.name + BACKUP_SUFFIX)
        try:
            wheels._write(backup, text)    # 每次覆盖成「引擎当前这一版」的原件
            wheels._write(f, new)
            changed += 1
        except OSError:
            pass
    if report:
        if changed:
            report("engine", None, f"已把 {len(hits)} 个 GitHub 下载地址改为国内直连")
        else:
            report("engine", None, "下载地址已是国内直连")
    return True


def restore() -> int:
    """还原成引擎原本的清单（诊断/排错用）"""
    n = 0
    for d in wheels._bootstrap_dirs():
        for name in NAMES:
            backup = d / (name + BACKUP_SUFFIX)
            if backup.is_file():
                try:
                    wheels._write(d / name, wheels._read(backup))
                    backup.unlink(missing_ok=True)   # 恢复成功后备份就没用了
                    n += 1
                except OSError:
                    pass
    return n


def status() -> dict:
    """给诊断用：哪些条目已被改写、备份在不在"""
    out: dict[str, object] = {"files": [], "entries": {}}
    for f in manifests():
        text = wheels._read(f)
        rewritten = sorted(u.split("/")[-1] for u in mirrors.SELF_HOSTED.values() if u in text)
        out["files"].append({                          # type: ignore[union-attr]
            "path": str(f),
            "backup": f.with_name(f.name + BACKUP_SUFFIX).is_file(),
            "rewritten": rewritten,
        })
    out["entries"] = {k: v for k, v in mirrors.SELF_HOSTED.items()}
    return out
