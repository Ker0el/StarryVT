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
import re
import shutil
import time
import urllib.request
from pathlib import Path

from job import Job, Cancelled
import mirrors
import wheels

#: 一次默认运行会下到的权重（照引擎的 PIPELINE_MODEL_IDS 抄，别自己加）
PIPELINE_MODEL_IDS = ("separator", "whisper", "qwen-referee")

#: 给用户看的名字（进度行里用）—— 直接写模型名，不藏
_LABEL = {
    "separator": "人声分离模型（BS-Roformer）",
    "whisper": "语音识别模型（faster-whisper large-v3-turbo）",
    "qwen-referee": "第二模型校验权重（Qwen3-ASR 0.6B）",
}

#: 单个模型最多下到多大（清单万一被改坏，不至于把装机拖死）
_MAX_BYTES = 3_000_000_000

#: 下载进度每 8MB 报一次（太密会把界面刷爆）
_PROGRESS_STEP = 8 << 20

_HF_HUB = Path("models") / "huggingface" / "hub"
_SEP_DIR = Path("models") / "audio-separator"

#: 我们在 HF 缓存目录里留的一份“这次到底下了哪些文件”的清单。
#: 有了它，`ready()` 只用 stat 就能判断齐不齐，不必每次翻译都去问一遍 API。
_SIDECAR = ".starryvt-files.json"

#: 加载器真正会打开的小文件（HF 仓库里有、但引擎清单为了省事没列的那些）。
#: 旧版预取只下了清单里的 model.bin，于是造出一个“看着完整”的 snapshot：
#: 引擎的完整性判据只认清单文件，会给 faster-whisper 传 local_files_only=True，
#: CTranslate2 随即报 `Cannot load the vocabulary from the model directory`。
#: 这里列的是**缺了必然加载不了**的几个；其余文件由 API 清单补齐。
_REQUIRED = {
    "whisper": ("config.json", "tokenizer.json", "vocabulary.json",
                "preprocessor_config.json"),
    "qwen-referee": ("config.json", "tokenizer_config.json", "processor_config.json",
                     "chat_template.jinja"),
}


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


def _hf_repo_files(repo: str, rev: str, params: dict) -> list[dict]:
    """问 HF API：这个仓库在钉住的版本上到底有哪些文件。

    ★ 这一步**不能省**。引擎的 model-manifest.json 只为每个模型钉了大权重
      （whisper 只有 model.bin），但加载器还要同目录的小文件：faster-whisper 要
      vocabulary.json / config.json / tokenizer.json，transformers 要 config.json /
      tokenizer_config.json / processor_config.json。只下大权重会造出一个
      “引擎认为完整、加载器打不开”的 snapshot（2026-10-19 客户报障）。

    API 的 `?blobs=true` 会给出 size 和 blobId / lfs.sha256 —— HF 缓存里
    普通文件的 blob 名是 git blob sha1（blobId），LFS 文件是 sha256。
    拿不到清单时返回空列表，调用方要**放弃预取**，别造残缺快照。
    """
    for ep in _endpoints(params):
        url = f"{ep}/api/models/{repo}/revision/{rev}?blobs=true"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "StarryVT"})
            with _opener(params).open(req, timeout=30) as r:
                data = json.load(r)
        except Exception:  # noqa: BLE001 - 换下一个源
            continue
        out = []
        for s in data.get("siblings") or []:
            name = s.get("rfilename")
            if not name:
                continue
            lfs = s.get("lfs") or {}
            out.append({
                "name": name,
                "size": int(s.get("size") or lfs.get("size") or 0),
                "lfs_sha256": (lfs.get("sha256") or "").lower(),
                "blob": (s.get("blobId") or "").lower(),
            })
        if out:
            return out
    return []


def _listed_only(spec: dict) -> bool:
    """引擎清单里的 `listedOnly`：只下清单里那几个文件（其余文件加载时自己会拿）"""
    return bool(spec.get("listedOnly") or spec.get("listed_only"))


def _drop_incomplete_snapshot(model_id: str, params: dict, report,
                              announce: bool = True) -> None:
    """把加载不了的 snapshot 目录删掉，让引擎自己按全仓库重下。

    只在**缺 `_REQUIRED` 小文件**时动手；blobs 一个都不删（1.6GB 的大权重会被
    huggingface_hub 认出来复用，只补几 MB 的小文件）。这是 API 清单拿不到时的兜底：
    留着那个 snapshot 的话，引擎会把它当完整快照、继续开 local_files_only。
    """
    spec = _models().get(model_id) or {}
    repo, rev = spec.get("repo"), spec.get("revision")
    required = _REQUIRED.get(model_id)
    if not (repo and rev and required):
        return
    snap = (_home(params) / _HF_HUB / ("models--" + repo.replace("/", "--"))
            / "snapshots" / rev)
    try:
        if all((snap / n).is_file() for n in required):
            return                              # 已经齐了，别动
        if snap.is_dir():
            shutil.rmtree(snap, ignore_errors=True)
            if announce:
                report("engine", None,
                       f"（{_LABEL.get(model_id, model_id)}缓存不完整，已交给引擎重新补齐）")
    except OSError:
        pass


def _plan(spec: dict, home: Path, params: dict,
          repo_files: list[dict] | None = None) -> tuple[list, list]:
    """算出这个模型要下哪些文件、下完还要建哪些硬链。

    返回 (files, links)：
      files = [(文件名, 落盘路径, 大小, sha256, [候选 URL…])]
      links = [(blob 路径, snapshot 路径)]   —— HF 类才有；引擎只认 snapshot 那儿
    清单不全就返回空。

    `repo_files` 给了就按 **仓库完整清单** 来（HF 类、且清单不是 listedOnly）——
    这是修复“只有 model.bin”的关键；不给就只排引擎清单里的那几个（ready() 用）。
    """
    files = spec.get("files") or []
    if not files:
        return [], []
    repo, rev = spec.get("repo"), spec.get("revision")
    if repo and rev:                                   # HF 类（whisper / qwen-referee）
        base = home / _HF_HUB / ("models--" + repo.replace("/", "--"))
        blob_dir, snap_dir = base / "blobs", base / "snapshots" / rev
        out, links, seen = [], [], set()
        manifest = {str(f.get("name")): f for f in files if f.get("name")}
        # 先按仓库清单排：大权重在前在后都无所谓，但小文件必须一个不缺
        for f in repo_files or []:
            name = f.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            m = manifest.get(name) or {}
            blob = f.get("lfs_sha256") or f.get("blob") or name
            sha = f.get("lfs_sha256") or (m.get("sha256") or "")
            out.append((name, blob_dir / blob, int(f.get("size") or 0), sha,
                        [f"{e}/{repo}/resolve/{rev}/{name}" for e in _endpoints(params)]))
            links.append((blob_dir / blob, snap_dir / name))
        for f in files:                                # 清单里漏在 API 结果外的，补上
            name = f.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
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
        # 镜像优先，但**保留官方地址兜底**（镜像返回错误页 / 断流时还能退回 GitHub）
        mirrored = mirrors.prefixed(url, params)
        urls = [mirrored] if mirrored == url else [mirrored, url]
        out.append((name, home / _SEP_DIR / name, int(f.get("size") or 0),
                    (f.get("sha256") or "").lower(), urls))
    return out, []


def _sidecar_ok(base: Path, snap_dir: Path) -> bool:
    """我们自己留的完整文件清单：每个文件都在 snapshot 里、大小对。

    没有这份清单 = 缓存是引擎（完整）或旧版（可能残缺）留下的，交给 `_REQUIRED` 兜。
    """
    p = base / _SIDECAR
    if not p.is_file():
        return True
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for it in data.get("files") or []:
            name = it.get("name")
            if not name:
                return False
            f = snap_dir / name
            if not f.is_file():
                return False
            want = int(it.get("size") or 0)
            if want and f.stat().st_size != want:
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


def ready(params: dict, model_id: str) -> bool:
    """这个模型的权重现在齐不齐（给诊断/界面用，不联网）。

    判据和引擎一致：文件在、大小对；HF 类还要 snapshot 里那几个链接也在。
    再补两条：加载器**必须**的小文件（`_REQUIRED`）在不在，以及我们自己的
    完整清单（`_SIDECAR`）能不能全部对上 —— 这两条专门用来识别旧版预取
    留下的“只有 model.bin、加载不了”的残废缓存。
    """
    home = _home(params)
    spec = _models().get(model_id) or {}
    files, links = _plan(spec, home, params)
    if not files:
        return False
    try:
        for _name, dest, want, _sha, _urls in files:
            if not dest.is_file() or (want and dest.stat().st_size != want):
                return False
        for _blob, snap in links:
            if not snap.is_file():
                return False
        repo, rev = spec.get("repo"), spec.get("revision")
        if repo and rev:
            base = home / _HF_HUB / ("models--" + repo.replace("/", "--"))
            snap_dir = base / "snapshots" / rev
            for name in _REQUIRED.get(model_id, ()):   # 加载器要读的小文件
                if not (snap_dir / name).is_file():
                    return False
            if not _sidecar_ok(base, snap_dir):
                return False
    except OSError:
        return False
    return True


# ---------- 下载 ----------

def _opener(params: dict):
    # ★ 用**实际可用**的代理：配置的那个死了会自动找系统代理 / 本机常见端口；
    #   都没有就强制直连（别让 urllib 自己去读那个已经关掉的系统代理）。
    return mirrors.opener(params or {})


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
    # 非 LFS 文件在 HF 缓存里的 blob 名 = git blob 的 sha1（清单里只有大权重的 sha256）。
    # git blob 哈希 = sha1("blob <字节数>\0" + 内容)，字节数就是清单里的大小。
    hb = hashlib.sha1(b"blob %d\0" % want) if want else None
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
                if hb is not None:
                    hb.update(chunk)
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
    bad = bool(sha and h.hexdigest().lower() != sha.lower())
    if (not bad and hb is not None and re.fullmatch(r"[0-9a-f]{40}", dest.name)
            and hb.hexdigest().lower() != dest.name):
        bad = True
    if bad:
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


def _write_sidecar(base: Path, rev: str, links: list) -> None:
    """记下这次到底下齐了哪些文件；`ready()` 之后只靠它 + stat 判断，不再联网。"""
    try:
        base.mkdir(parents=True, exist_ok=True)
        items = []
        for blob, snap in links:
            try:
                size = blob.stat().st_size
            except OSError:
                size = 0
            items.append({"name": snap.name, "blob": blob.name, "size": size})
        (base / _SIDECAR).write_text(
            json.dumps({"revision": rev, "files": items}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


# ---------- 入口 ----------

def prefetch_one(model_id: str, params: dict, job: Job, report,
                 announce: bool = True) -> bool:
    """把一个模型的权重下齐。已经齐了就直接返回（真返回时只是一次 stat）。"""
    home = _home(params)
    spec = _models().get(model_id) or {}
    files, links = _plan(spec, home, params)
    total = sum(f[2] for f in files)
    if not files or not total or total > _MAX_BYTES:
        return False
    label = _LABEL.get(model_id, model_id)
    if ready(params, model_id):
        return True

    repo, rev = spec.get("repo"), spec.get("revision")
    if repo and rev and not _listed_only(spec):
        # ★ 必须按**仓库完整清单**下：引擎的清单只钉了大权重，缺了小文件
        #   （vocabulary.json / config.json …）加载器就打不开，而引擎还会
        #   因为这个 snapshot“看起来完整”而开 local_files_only。见 _REQUIRED。
        repo_files = _hf_repo_files(repo, rev, params)
        if not repo_files:
            # 拿不到完整清单就**什么都不下、也不建 snapshot**：让引擎自己按
            # 老路子（snapshot_download 全仓库）去拿。造一个残缺快照比不预取更糟。
            _drop_incomplete_snapshot(model_id, params, report, announce)
            if announce:
                report("engine", None,
                       f"（{label}跳过预取：没拿到完整文件清单，交给引擎自己下载）")
            return False
        files, links = _plan(spec, home, params, repo_files=repo_files)
        total = sum(f[2] for f in files)
        if not files or not total or total > _MAX_BYTES:
            _drop_incomplete_snapshot(model_id, params, report, announce)
            return False

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
            _drop_incomplete_snapshot(model_id, params, report, announce)
            return False
    for blob, snap in links:          # HF 类：blob 链进 snapshots/<rev>/（引擎只看那儿）
        _link(blob, snap)
    if repo and rev:
        _write_sidecar(home / _HF_HUB / ("models--" + repo.replace("/", "--")), rev, links)
    if announce:
        report("engine", None, f"✅ {label}已就绪")
    return True


def repair_pipeline(params: dict, job: Job, report) -> None:
    """开跑前补一次模型缓存（齐了就是几次 stat，不联网）。

    专门收拾**旧版预取留下的残废快照**：2026-10-19 客户报障的
    `Cannot load the vocabulary from the model directory`，就是旧版只下了
    model.bin、没下 vocabulary.json 造成的。用户不用清缓存重下几个 GB，
    这里把缺的小文件补进去就行。
    """
    for mid in PIPELINE_MODEL_IDS:
        job.check()
        try:
            if ready(params, mid):
                continue
            report("translate", None, f"检查并补全{_LABEL.get(mid, mid)}缓存…")
            prefetch_one(mid, params, job, report, announce=False)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 补不齐也让引擎自己去试，别拦任务
            report("translate", None, f"（{_LABEL.get(mid, mid)}缓存补全跳过：{e}）")


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
