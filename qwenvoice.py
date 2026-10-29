#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 Qwen3-TTS（0.6B）：可选按需安装的配音引擎。

不替代任何现有方案：克隆引擎里是第三项（云端 MiMo / Index / Qwen）。

关键设计（2026-10-29 实测后改的）：
  · Qwen3-TTS 要 transformers 4.57.3，IndexTTS 只认 4.52.1 —— 装同一个 venv 会互相破坏。
  · 但**翻译引擎自己的 venv 里已经有 torch 2.11+cu128 和 CUDA**（Python 3.12）。
    所以这里不再下载一份 3.4 GB 的 torch，而是把 qwen-tts 和它要的 transformers
    用 `uv pip install --target` 装到单独目录，运行时给引擎的 python 加 PYTHONPATH。
    —— 不改引擎环境，也不重复下 torch；安装只有几十 MB + 两个模型（约 5 GB）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import diag
import paths
from job import Job, no_window_kwargs

#: CustomVoice 模型的 9 个内置音色
VOICES = ("aiden", "dylan", "eric", "ono_anna", "ryan",
          "serena", "sohee", "uncle_fu", "vivian")

BASE_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
CUSTOM_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
BATCH = 6                     # 实测 6 句一批最快；18 句会退化
MODEL_FILES = ("config.json", "model.safetensors",
               "speech_tokenizer/config.json",
               "speech_tokenizer/model.safetensors",
               "speech_tokenizer/preprocessor_config.json")

#: --target 安装的依赖（torch 由引擎运行时提供，不在这里）
_DEPS = ("qwen-tts==0.1.1", "transformers==4.57.3", "tokenizers==0.22.1",
         "accelerate==1.12.0", "huggingface_hub==0.35.0", "sox", "requests")
_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
_MS_API = ("https://modelscope.cn/api/v1/models/{repo}/repo/files"
           "?Revision=master&Recursive=true")
_MS_FILE = "https://modelscope.cn/models/{repo}/resolve/master/{path}"


def home() -> Path:
    raw = (os.environ.get("STARRYVT_QWEN_HOME") or "").strip()
    return Path(raw) if raw else paths.resolve("qwen-tts")


def site_dir() -> Path:
    """--target 安装的依赖目录（运行时加到 PYTHONPATH）"""
    return home() / "site"


def model_dir(kind: str = "base") -> Path:
    over = (os.environ.get("STARRYVT_QWEN_BASE" if kind == "base"
                           else "STARRYVT_QWEN_CUSTOM") or "").strip()
    if over:
        return Path(over)
    name = "Qwen3-TTS-0.6B-Base" if kind == "base" else "Qwen3-TTS-0.6B-CustomVoice"
    return home() / "models" / name


def _dev_python() -> Path | None:
    raw = (os.environ.get("STARRYVT_QWEN_PY") or "").strip()
    p = Path(raw) if raw else None
    return p if p and p.is_file() else None


def engine_python() -> Path | None:
    """翻译引擎运行时里的 python（自带 torch cu128）。"""
    try:
        import settings as cfg
        import datadir
        params = cfg.load()
        base = Path((params.get("finesub_home") or "").strip() or datadir.default_home())
        p = base / "runtime" / "python" / "Scripts" / "python.exe"
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def python_exe() -> Path | None:
    return _dev_python() or engine_python()


def _missing(d: Path) -> list[str]:
    return [n for n in MODEL_FILES if not (d / n).is_file()]


def status() -> dict:
    py = python_exe()
    site_ok = (site_dir() / "qwen_tts").is_dir() and (site_dir() / "transformers").is_dir()
    miss_base = _missing(model_dir("base"))
    miss_custom = _missing(model_dir("custom"))
    base_ok, custom_ok = not miss_base, not miss_custom
    ready = bool(py) and site_ok and base_ok and custom_ok
    if ready:
        detail = f"已装好（{home()}）"
    elif not engine_python() and not _dev_python():
        detail = "需要先安装翻译引擎（Qwen 复用引擎里的 torch 运行时）"
    elif not site_ok:
        detail = f"没装（依赖几十 MB + 两个模型约 5 GB）。将装在：{home()}"
    else:
        miss = miss_base or miss_custom
        detail = f"依赖好了，模型还缺：{', '.join(miss[:3])}"
    return {"ready": ready, "installed": site_ok, "base_ready": base_ok,
            "custom_ready": custom_ok, "missing": miss_base or miss_custom,
            "detail": detail, "dir": str(home())}


# ---------------- 下载（ModelScope 直连，多线程分块） ----------------

def _opener() -> urllib.request.OpenerDirector:
    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    o.addheaders = [("User-Agent", "StarryVT/1.0")]
    return o


def _one_range(url: str, part: Path, start: int, end: int) -> None:
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            with _opener().open(req, timeout=120) as r:
                data = r.read()
            if len(data) != end - start + 1:
                raise IOError(f"short read {len(data)}")
            with open(part, "r+b") as f:
                f.seek(start)
                f.write(data)
            return
        except Exception:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))


def _download(repo: str, path: str, size: int, dest: Path,
              job: Job, report) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == size:
        return
    part = dest.with_name(dest.name + ".part")
    url = _MS_FILE.format(repo=repo, path=urllib.parse.quote(path))
    with open(part, "wb") as f:
        f.truncate(size)
    chunk = 8 << 20
    spans = [(s, min(s + chunk - 1, size - 1)) for s in range(0, size, chunk)]
    report("clone", None, f"下载 {path}（{size / 1048576:.0f} MB）…")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda a: _one_range(url, part, a[0], a[1]), spans))
    if part.stat().st_size != size:
        raise RuntimeError(f"{path} 大小不对")
    os.replace(part, dest)


def _fetch_models(repo: str, dest: Path, job: Job, report) -> None:
    with _opener().open(_MS_API.format(repo=repo), timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    files = [f for f in data["Data"]["Files"] if f.get("Type") == "blob"]
    for f in files:
        job.check()
        _download(repo, f["Path"], int(f.get("Size") or 0), dest / f["Path"], job, report)


# ---------------- 安装 ----------------

def install(params: dict, job: Job, report) -> dict:
    py = engine_python()
    if not py:
        raise RuntimeError("Qwen3-TTS 复用翻译引擎的 torch 运行时：请先在首页把翻译引擎装好")
    import engine
    uv = engine.find_uv() or engine.install_uv(params, job, report)
    if not uv:
        raise RuntimeError("找不到 uv，安装 Qwen3-TTS 需要它")

    site_dir().mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env["NO_PROXY"] = "*"
    env["UV_LINK_MODE"] = "copy"

    def run(cmd: list[str], what: str) -> None:
        job.check()
        report("clone", None, what)
        r = subprocess.run([str(uv), *cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           **no_window_kwargs())
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-600:]
            raise RuntimeError(f"{what}失败：{tail}")

    if not (site_dir() / "qwen_tts").is_dir():
        run(["pip", "install", "--target", str(site_dir()), "--no-deps",
             "--default-index", _INDEX, *_DEPS], "安装 Qwen3-TTS 依赖（约 50 MB）…")

    _fetch_models(BASE_REPO, model_dir("base"), job, report)
    _fetch_models(CUSTOM_REPO, model_dir("custom"), job, report)
    st = status()
    if not st["ready"]:
        raise RuntimeError(f"装完了但状态不对：{st['detail']}")
    report("clone", 100, "Qwen3-TTS 已装好")
    return st


# ---------------- 合成 ----------------

def synth(items: list[tuple[str, Path, str]], ref: Path | str | None,
          params: dict, job: Job, work: Path, progress_cb=None,
          preset: bool = False) -> dict:
    st = status()
    if not st["ready"]:
        raise RuntimeError(f"Qwen3-TTS 还没装好：{st['detail']}")
    if not items:
        return {"ok": 0, "fail": 0, "errors": []}

    spec = {
        "model_dir": str(model_dir("base")),
        "custom_dir": str(model_dir("custom")),
        "ref": str(ref or ""),
        "ref_text": str(params.get("qwen_ref_text") or ""),
        "batch": BATCH,
        "jobs": [{"text": t, "out": str(p), "speaker": s} for t, p, s in items],
    }
    spec_path = Path(work) / "qwen_jobs.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    if not (base / "qwen_synth.py").is_file():
        base = Path(__file__).parent
    env = dict(os.environ)
    # ★ 引擎 python 是 venv；某些环境（PYTHONHOME 被父进程改过、pyvenv.cfg 异常）下
    #   它会读不到自己的 site-packages，于是 huggingface_hub 导入 requests 失败。
    #   这里显式把引擎的 site-packages 加进 PYTHONPATH，并清掉会改解释器根目录的变量。
    eng_site = python_exe().parent.parent / "Lib" / "site-packages"
    paths = [str(site_dir())] + ([str(eng_site)] if eng_site.is_dir() else [])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env["NO_PROXY"] = "*"
    cmd = [str(python_exe()), str(base / "qwen_synth.py"), str(spec_path)]
    diag.write(f"[qwen] py={python_exe()} site={site_dir()} "
               f"eng_site_ok={eng_site.is_dir()} PYTHONPATH={env.get('PYTHONPATH')}", "run")
    p = job.spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True, encoding="utf-8", errors="replace",
                  cwd=str(work), env=env, **no_window_kwargs())
    total = len(items)
    done = 0
    errors: list[str] = []
    tail: list[str] = []
    for line in p.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
        m = re.match(r"\[(\d+)\]\s+(ok|fail)\b(.*)", line)
        if m:
            n = int(m.group(1))
            if m.group(2) == "ok":
                done = max(done, n)
                if progress_cb:
                    progress_cb(min(done, total), total)
            else:
                errors.append(line)
        elif line.startswith("DONE"):
            diag.write(f"[qwen] {line}", "run")
    code = p.wait()
    job.check()
    if done < total:
        diag.write("[qwen] child tail: " + " | ".join(tail[-12:]), "run")
        errors.append(tail[-1] if tail else f"退出码 {code}")
        raise RuntimeError(f"Qwen3-TTS 合成失败 {total - done} 句：{errors[0]}")
    return {"ok": done, "fail": 0, "errors": []}
