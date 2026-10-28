#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 Qwen3-TTS（0.6B）：可选按需安装的配音引擎。

和 IndexTTS（localclone.py）并列，不替代任何现有方案：
  - 预设音色：Qwen3-TTS-0.6B-CustomVoice 的 9 个内置音色
  - 声音克隆：Qwen3-TTS-0.6B-Base，3~7 秒参考音（英文参考音也能念中文）

为什么单开一套运行时：Qwen3-TTS 要求 transformers 4.57，IndexTTS 只认 4.52，
装在一起会互相破坏（实测）。所以这里自己一个 venv，按需下载。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import diag
import paths
from job import Job, no_window_kwargs

#: CustomVoice 模型的 9 个内置音色
VOICES = ("aiden", "dylan", "eric", "ono_anna", "ryan",
          "serena", "sohee", "uncle_fu", "vivian")

#: 模型仓库（ModelScope；HF 作为兜底）
BASE_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
CUSTOM_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

BATCH = 6          # 实测 6 句一批最快；18 句会退化
MODEL_FILES = ("config.json", "model.safetensors",
               "speech_tokenizer/config.json",
               "speech_tokenizer/model.safetensors",
               "speech_tokenizer/preprocessor_config.json")


def home() -> Path:
    raw = (os.environ.get("STARRYVT_QWEN_HOME") or "").strip()
    return Path(raw) if raw else paths.resolve("qwen-tts")


def runtime() -> Path:
    return home() / "runtime"


def python_exe() -> Path:
    return runtime() / "Scripts" / "python.exe"


def model_dir(kind: str = "base") -> Path:
    over = (os.environ.get("STARRYVT_QWEN_BASE" if kind == "base" else "STARRYVT_QWEN_CUSTOM") or "").strip()
    if over:
        return Path(over)
    name = "Qwen3-TTS-0.6B-Base" if kind == "base" else "Qwen3-TTS-0.6B-CustomVoice"
    return home() / "models" / name


def _app_dir() -> Path:
    """辅助脚本所在目录：打包后是 StarryVT.exe 旁边（和 localclone.APP_DIR 一致）"""
    import tools
    if tools.is_packaged():
        return tools.exec_path().parent
    return Path(__file__).parent


def _script() -> Path:
    return _app_dir() / "qwen_synth.py"


def _missing(d: Path) -> list[str]:
    return [n for n in MODEL_FILES if not (d / n).is_file()]


def status() -> dict:
    """装到哪一步：{ready, installed, base_ready, custom_ready, missing, detail}"""
    env_ok = bool(_dev_python()) or python_exe().is_file()
    miss_base = _missing(model_dir("base"))
    miss_custom = _missing(model_dir("custom"))
    base_ok = not miss_base
    custom_ok = not miss_custom
    if env_ok and base_ok and custom_ok:
        detail = f"已装好（{home()}）"
    elif env_ok:
        miss = miss_base or miss_custom
        detail = f"运行时好了，模型还缺：{', '.join(miss[:3])}"
    else:
        detail = f"没装（约 6 GB：运行时 + 两个 0.6B 模型）。将装在：{home()}"
    return {"ready": env_ok and base_ok and custom_ok,
            "installed": env_ok, "base_ready": base_ok,
            "custom_ready": custom_ok, "missing": miss_base or miss_custom,
            "detail": detail, "dir": str(home())}


def install(params: dict, job: Job, report) -> dict:
    """按需安装运行时 + 两个模型（约 6 GB）。"""
    st = status()
    if st["ready"]:
        return st
    import shutil
    import subprocess

    import engine

    uv = engine.find_uv()
    if not uv:
        uv = engine.install_uv(params, job, report)
    if not uv:
        raise RuntimeError("找不到 uv，安装 Qwen3-TTS 需要它（点一次「安装翻译引擎」会自动准备好）")
    home().mkdir(parents=True, exist_ok=True)
    env = engine.uv_env(params)
    env["UV_PYTHON_INSTALL_DIR"] = str(home() / "uv-python")
    env["UV_LINK_MODE"] = "copy"
    from job import no_window_kwargs

    def run(cmd: list[str], what: str) -> None:
        job.check()
        report("clone", None, what)
        r = subprocess.run([str(uv), *cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           **no_window_kwargs())
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-600:]
            raise RuntimeError(f"{what}失败：{tail}")

    if not python_exe().is_file():
        run(["venv", str(runtime()), "--python", "3.12"], "创建 Qwen 运行环境…")

    # 有 NVIDIA 显卡就装 CUDA 版 torch（约 2.5 GB）；否则装 CPU 版
    try:
        import doctor
        has_nv = bool((doctor._gpu_info() or {}).get("name"))
    except Exception:  # noqa: BLE001
        has_nv = False
    if has_nv:
        run(["pip", "install", "--python", str(python_exe()), "torch", "torchaudio",
             "--index-url", "https://download.pytorch.org/whl/cu128"],
            "安装 GPU 运行时（torch cu128，约 2.5 GB）…")
    else:
        run(["pip", "install", "--python", str(python_exe()), "torch", "torchaudio"], "安装 CPU 运行时…")

    run(["pip", "install", "--python", str(python_exe()),
         "qwen-tts==0.1.1", "--no-deps"], "安装 qwen-tts…")
    run(["pip", "install", "--python", str(python_exe()),
         "transformers==4.57.3", "tokenizers==0.22.1", "accelerate==1.12.0",
         "librosa", "soundfile", "sox", "onnxruntime", "einops",
         "modelscope", "numpy", "huggingface_hub", "tqdm", "requests"],
        "安装 Qwen3-TTS 依赖…")

    spec = {"repos": [[BASE_REPO, str(model_dir("base"))],
                      [CUSTOM_REPO, str(model_dir("custom"))]]}
    spec_path = home() / "fetch.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    script = _app_dir() / "qwen_fetch.py"
    report("clone", None, "下载 Qwen3-TTS 模型（约 5 GB，国内直连）…")
    env2 = dict(env)
    env2["NO_PROXY"] = "*"
    env2.pop("HTTP_PROXY", None)
    env2.pop("HTTPS_PROXY", None)
    r = subprocess.run([str(python_exe()), str(script), str(spec_path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env2, **no_window_kwargs())
    if r.returncode != 0:
        raise RuntimeError(f"模型下载失败：{(r.stderr or r.stdout or '')[-600:]}")
    st = status()
    if not st["ready"]:
        raise RuntimeError(f"装完了但状态不对：{st['detail']}")
    report("clone", 100, "Qwen3-TTS 已装好")
    return st


def _dev_python() -> Path | None:
    """开发/测试用：环境变量指向已有运行时，跳过安装。"""
    raw = (os.environ.get("STARRYVT_QWEN_PY") or "").strip()
    p = Path(raw) if raw else None
    return p if p and p.is_file() else None


def _python() -> Path:
    return _dev_python() or python_exe()


def synth(items: list[tuple[str, Path, str]], ref: Path | str | None,
          params: dict, job: Job, work: Path, progress_cb=None,
          preset: bool = False) -> dict:
    """整批合成。items = [(文本, 输出 wav, speaker)]；preset=True 时用内置音色。

    返回 {"ok": n, "fail": n, "errors": [...]}；失败会抛 RuntimeError。
    """
    st = status()
    if not preset and not st["base_ready"]:
        raise RuntimeError("Qwen3-TTS 的克隆模型还没装好，请到 设置 → 配音 → Qwen3-TTS（本地）下载")
    if preset and not st["custom_ready"]:
        raise RuntimeError("Qwen3-TTS 的内置音色模型还没装好，请先下载")
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

    cmd = [str(_python()), str(_script()), str(spec_path)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
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
    fail = total - done
    if code != 0 and fail == 0:
        fail = 1
        errors.append(tail[-1] if tail else f"退出码 {code}")
    if fail:
        raise RuntimeError(f"Qwen3-TTS 合成失败 {fail} 句：{errors[0] if errors else ''}")
    return {"ok": done, "fail": 0, "errors": []}
