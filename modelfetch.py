#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""装引擎时把三个模型权重一次下齐：人声分离 / 语音识别 / 第二模型校验

**为什么要在这一步下**：引擎默认是「用到才下」，而下那一步**全程静默** ——
读 vendored 源码确认：`hf_weights.prepare` 那句提示走 `reporter.debug()`（normal 级别
不打印），真正的下载跑在 `capture_output` 的子进程里（进度条全被吞掉），下载失败也被
吞掉。后果实测过两次：

  · 2026-10-08 的客户在第一次翻译那儿反复关掉软件重开，留下一堆没退干净的引擎进程
    互相抢文件锁，把后面的失败堆成一串；
  · 2026-10-13 本机：校验权重下到 1.2GB 断了，此后每段视频白花 5 秒去加载一个不存在
    的模型，什么也没复核。

用户点「一键安装引擎」是自愿的、进度就摆在他眼前，这时候下最合适 ——
2026-10-16 用户定：「我就是要初始化的时候都下好」。

**下哪些**：照引擎自己声明的「桌面版跑一次默认会下到的权重」来，不自己拍 ——
`finesub_bootstrap/model_caches.py: PIPELINE_MODEL_IDS = ("separator", "whisper",
"qwen-referee")` ≈ 3.7 GB。日语语音模型（2.9 GB）**不在其中**：只有显式选了日语才用
得到，别替所有人下。

**怎么下**：照引擎自带的 `model-manifest.json` 走（钉着 revision 和每个文件的
sha256），所以不用认识 `huggingface_hub`（打包少背一个依赖）。两种落盘布局：

  · HF 类（whisper / qwen-referee）：标准 HF 缓存布局 —— `blobs/<sha256>` 是真身，
    `snapshots/<revision>/<文件名>` 硬链过去（同盘不占第二份）。引擎的判据是
    「snapshot 非空 + 每个文件大小对得上」，硬链能过。
  · separator（人声分离）：清单里带完整 url，平铺进 `models/audio-separator/<文件名>`。

镜像下不动就退回官方源（2026-10-13 实测 hf-mirror 会抽风，而官方直连在国内是死的）。

**失败不算失败**：某一样下不动就记一行日志、继续下一样 —— 引擎用到时还会自己再试
一次；这一层是锦上添花，绝不能把「装引擎」这一步判死。
（本模块的前身是 `referee.py`，只管校验那一个模型；2026-10-16 泛化成三个。）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

from job import Job
import wheels

#: 一次默认运行会下到的权重（照引擎的 PIPELINE_MODEL_IDS 抄，别自己加）
PIPELINE_MODEL_IDS = ("separator", "whisper", "qwen-referee")

#: 给用户看的名字（进度行里用）
_LABEL = {
    "separator": "人声分离模型",
    "whisper": "语音识别模型",
    "qwen-referee": "第二模型校验权重",
}

#: 单个模型最多下到多大（清单万一被改坏，不至于把装机拖死）
_MAX_BYTES = 3_000_000_000

#: 下载进度每 8MB 报一次（太密会把界面刷爆）
_PROGRESS_STEP = 8 << 20

_HF_HUB = Path("models") / "huggingface" / "hub"
_SEP_DIR = Path("models") / "audio-separator"


# ---------- 清单 / 路径 ----------

def _models() -> dict:
    """引擎自带的 `model-manifest.json` 里的 models 段"""
    for d in wheels._bootstrap_dirs():
        try:
            p = d / "model-manifest.json"
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8")).get("models") or {}
        except Exception:  # noqa: BLE001
            continue
    return {}


def _home(params: dict) -> Path:
    if (params or {}).get("finesub_home"):
        return Path(params["finesub_home"])
    return Path(os.environ.get("LOCALAPPDATA", "")) / "FineSub"


def _endpoints(params: dict) -> list[str]:
    """HF 类的下载源：镜像优先，失败退官方（官方在国内直连基本是死的，但对有代理的人可用）"""
    out = [(params.get("hf_endpoint") or "https://hf-mirror.com").rstrip("/")]
    if "huggingface.co" not in out[0]:
        out.append("https://huggingface.co")
    return out


def _plan(spec: dict, home: Path, params: dict) -> tuple[list, list]:
    """算出这个模型要下哪些文件、下完还要建哪些硬链。

    返回 (files, links)：
      files = [(文件名, 落盘路径, 大小, sha256, [候选 URL…])]
      links = [(blob 路径, snapshot 路径)]   —— HF 类才有；引擎只认 snapshot 那儿
    清单不全就返回空。
    """
    files = spec.get("files") or []
    if not files:
        return [], []
    repo, rev = spec.get("repo"), spec.get("revision")
    if repo and rev:                                   # HF 类（whisper / qwen-referee）
        base = home / _HF_HUB / ("models--" + repo.replace("/", "--"))
        blob_dir, snap_dir = base / "blobs", base / "snapshots" / rev
        out, links = [], []
        for f in files:
            name = f.get("name")
            if not name:
                continue
            sha = (f.get("sha256") or "").lower()
            blob = blob_dir / (sha or name)            # HF 的 blob 名就是 sha256
            out.append((name, blob, int(f.get("size") or 0), sha,
                        [f"{e}/{repo}/resolve/{rev}/{name}" for e in _endpoints(params)]))
            links.append((blob, snap_dir / name))
        return out, links
    # 平铺类（separator）：清单里带完整 url，直接落进它自己的目录
    out = []
    for f in files:
        name, url = f.get("name"), f.get("url")
        if not (name and url):
            continue
        out.append((name, home / _SEP_DIR / name, int(f.get("size") or 0),
                    (f.get("sha256") or "").lower(), [url]))
    return out, []


def ready(params: dict, model_id: str) -> bool:
    """这个模型的权重现在齐不齐（给诊断/界面用，不联网）。

    判据和引擎一致：文件在、大小对；HF 类还要 snapshot 里那几个链接也在。
    """
    home = _home(params)
    files, links = _plan(_models().get(model_id) or {}, home, params)
    if not files:
        return False
    try:
        for _name, dest, want, _sha, _urls in files:
            if not dest.is_file() or (want and dest.stat().st_size != want):
                return False
        for _blob, snap in links:
            if not snap.is_file():
                return False
    except OSError:
        return False
    return True


# ---------- 下载 ----------

def _opener(params: dict):
    proxy = (params or {}).get("proxy") or ""
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _fetch(url: str, dest: Path, want: int, sha: str, params: dict, job: Job,
           report, label: str) -> bool:
    """下到 dest 并核对 sha256。先写 `.part`，验过了才改名——中断不留半个"真身"。

    HF 的 blob 文件名本身就是它的 sha256，所以引擎那边的判据（存在 + 大小对）天然成立；
    我们多算一遍哈希，是为了防「字节能下、内容错了」这种最难查的情况。
    """
    part = dest.with_name(dest.name + ".part")
    # ⚠️ 父目录要先建出来：以前这一步挪在最后（改名之前），生产里靠引擎/mkdir 恰好先把
    #    `blobs/` 建好了才没炸；换个模型（比如平铺进 audio-separator 的那种）就是
    #    FileNotFoundError，而且报的是"没下成"，看着像网络问题（2026-10-16 单测抓到）。
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        part.unlink()
    except OSError:
        pass
    started, got, last = time.time(), 0, 0
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StarryVT"})
        with _opener(params).open(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                job.check()                       # 用户点了取消立刻走
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if got - last >= _PROGRESS_STEP:
                    last = got
                    pct = f"（{got * 100 // want}%）" if want else ""
                    speed = got / 1048576 / max(0.5, time.time() - started)
                    report("engine", None,
                           f"⬇ {label} {got // 1048576} MB"
                           + (f" / {want // 1048576} MB" if want else "")
                           + f"{pct} · {speed:.1f} MB/s")
    except Exception as e:  # noqa: BLE001
        report("engine", None,
               f"（{label} 没下成：{type(e).__name__}——引擎用到时会自己再试一次）")
        try:
            part.unlink()
        except OSError:
            pass
        return False
    if sha and h.hexdigest().lower() != sha.lower():
        report("engine", None, f"（{label} 校验不通过，已丢弃——稍后引擎会自己重下）")
        try:
            part.unlink()
        except OSError:
            pass
        return False
    try:
        part.replace(dest)
    except OSError:
        return False
    return True


def _link(blob: Path, snap: Path) -> None:
    """snapshot 指向 blob：同盘硬链（不多占空间），不行就复制一份"""
    try:
        snap.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        os.link(blob, snap)
        return
    except OSError:
        pass
    try:
        shutil.copyfile(blob, snap)
    except OSError:
        pass


# ---------- 入口 ----------

def prefetch_one(model_id: str, params: dict, job: Job, report,
                 announce: bool = True) -> bool:
    """把一个模型的权重下齐。已经齐了就直接返回（真返回时只是一次 stat）。"""
    home = _home(params)
    files, links = _plan(_models().get(model_id) or {}, home, params)
    total = sum(f[2] for f in files)
    if not files or not total or total > _MAX_BYTES:
        return False
    label = _LABEL.get(model_id, model_id)
    if ready(params, model_id):
        return True
    if announce:
        report("engine", None, f"预取{label}（约 {total // 1048576} MB）…")

    for name, dest, want, sha, urls in files:
        if dest.is_file() and (not want or dest.stat().st_size == want):
            continue
        got = False
        for url in urls:
            if _fetch(url, dest, want, sha, params, job, report, f"{label} {name}"):
                got = True
                break
        if not got:
            return False
    for blob, snap in links:          # HF 类：blob 链进 snapshots/<rev>/（引擎只看那儿）
        _link(blob, snap)
    if announce:
        report("engine", None, f"✅ {label}已就绪")
    return True


def prefetch_all(params: dict, job: Job, report) -> dict:
    """把默认运行会用到的权重都下齐。返回 {模型: True/False}；单个失败不影响别的。"""
    out: dict = {}
    for i, mid in enumerate(PIPELINE_MODEL_IDS):
        job.check()
        try:
            out[mid] = prefetch_one(mid, params, job, report)
        except Exception as e:  # noqa: BLE001 - 单个模型出问题不拖累其它
            out[mid] = False
            if report:
                report("engine", None, f"（{_LABEL.get(mid, mid)} 预取跳过：{e}）")
    if report and all(out.values()):
        report("engine", None, "✅ 模型权重已备齐（识别 / 分离 / 校验）")
    elif report:
        miss = "、".join(_LABEL.get(k, k) for k, ok in out.items() if not ok)
        report("engine", None, f"⚠ 这些权重这次没下齐：{miss}（第一次翻译时会自动再试）")
    return out
