#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地语音克隆（备用音色）：用 IndexTTS-2.5 在本机复刻原说话人的音色来念中文。

**为什么挑它**（2026-10-14 逐个比过一轮）
  - 中文零样本克隆里综合最好的一档：多方评测里韵律 / 音色 / 质量三项都压过
    CosyVoice2、F5-TTS、Fish-Speech（IndexTTS 一代 AVG 4.01，CosyVoice2 3.81；
    二代之后又高了）
  - **原生支持 Windows**：官方 CI 里有 Windows 任务，pyproject 专门为 win 准备了
    triton-windows 和 cu128 的 torch；装法就是 uv，跟引擎那套一模一样
  - 有 `duration_factor`（0.5~2.0 倍速）—— 中文要塞进原来的时间窗时用它，
    比事后做 atempo 变速自然得多（这正是用户抱怨的「突然加速」那件事）
  - 8 GB 显存就能跑（bf16），这台 4070 Ti（12 GB）绰绰有余
  - 许可：bilibili 模型使用许可（见仓库 LICENSE / LICENSE_ZH.txt）—— 免费、
    可商用（月活不过亿、年收入不过亿即可），副本要保留许可声明

**装在哪**：`%LOCALAPPDATA%\\StarryVT\\voiceclone\\`（自己的目录，**绝不碰引擎那套环境**，
引擎的依赖是钉死的，混在一起早晚炸）

**默认不装**：设置里点「下载并启用」才动手（环境约 2.5 GB + 模型约 4 GB）。
权重从 HuggingFace 拉，走设置里的 hf_endpoint 镜像和代理；断了再点一次会接着下。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import diag
import engine
import mirrors
import tools
from job import Job

HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StarryVT" / "voiceclone"
SRC = HOME / "index-tts"                 # 源码 + .venv 都在这儿
CKPT = SRC / "checkpoints"               # 模型权重
# 两个辅助脚本跟主程序一起走（build_nuitka.py 的 --include-data-file 和
# _rebuild_backend.py 的 --add-data 里都列了它们；那份 StarryVT.spec 早就没人用了）。
APP_DIR = (Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
           if tools.is_packaged() else Path(__file__).parent)
# ★★ 它们必须待在**自己的子目录**里（打包时也映射到 <APP_DIR>\clone\）。
#    这两个脚本是拿**克隆环境那个 3.11 的 python** 跑的，而 sys.path[0] 就是脚本
#    所在目录 —— 主程序目录里躺着 python312.dll / _ctypes.pyd（PyInstaller 的
#    _internal、Nuitka 的 app.dist、装完的 backend\ 都有），3.11 解释器一
#    `import _ctypes` 就会命中 3.12 那个，当场报
#    「Module use of python312.dll conflicts with this version of Python」。
#    单独一个目录里只有这两个 .py，不会有这个问题。（2026-10-15 实测踩到。）
SCRIPT_DIR = APP_DIR / "clone"

MODEL_REPO = "IndexTeam/IndexTTS-2.5"
SRC_ZIP = "https://github.com/index-tts/index-tts/archive/refs/heads/main.zip"
# 情感模型（qwen0.6bemo4-merge，1.1 GB）只有 use_emo_text 用得上，我们不用 —— 别下
SKIP = ("qwen0.6bemo4-merge/*",)


def python_exe() -> Path:
    """克隆环境自己的 python（跟主程序那套是两个世界）"""
    return SRC / ".venv" / "Scripts" / "python.exe"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def status() -> dict:
    """装到哪一步了：{ready, installed, model_ready, dir, model_gb, detail}"""
    env_ok = python_exe().is_file()
    model_ok = (CKPT / "config.yaml").is_file() and (CKPT / "gpt.pth").is_file()
    gb = round(_dir_size(CKPT) / 1024 ** 3, 1) if CKPT.is_dir() else 0.0

    if env_ok and model_ok:
        detail = f"已装好（模型 {gb} GB），随时可用" if gb else "已装好，随时可用"
    elif env_ok:
        detail = "运行环境装好了，模型还没下完"
    elif SRC.is_dir():
        detail = "上次没装完，点右边的按钮接着装"
    else:
        # 实测：运行环境 8.2 GB（torch+CUDA 那一套）+ 模型 ~6.8 GB
        detail = ("没装（约 15 GB：运行环境 8 G + 模型 7 G；"
                  "装在软件自己的数据目录里，不影响别的东西）")

    return {"ready": env_ok and model_ok, "installed": env_ok, "model_ready": model_ok,
            "dir": str(HOME), "model_gb": gb, "detail": detail}


def _sub_env(params: dict) -> dict:
    """跑子进程用的环境：代理 + HF 镜像 + 强制 UTF-8 输出"""
    env = dict(os.environ)
    proxy = (params or {}).get("proxy") or ""
    if proxy and mirrors.proxy_ok(params):
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    endpoint = ((params or {}).get("hf_endpoint") or "").strip()
    if endpoint:
        env["HF_ENDPOINT"] = endpoint          # 官方源下不动时用镜像（设置里那一项）
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run_streamed(job: Job, cmd: list[str], *, cwd: Path, env: dict, report, stage: str,
                  tag: str) -> None:
    """跑一条会自己吐进度的子进程（uv sync / 模型下载），每行都转给界面"""
    p = job.spawn(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                  errors="replace", bufsize=1)
    tail: list[str] = []
    for line in p.stdout or ():
        job.check()
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-40:]
        report(stage, None, f"[{tag}] {line}")
    code = p.wait()
    if code != 0:
        detail = "\n".join(tail[-15:])
        raise RuntimeError(f"{tag} 失败（退出码 {code}）：\n{detail}")


def _unzip_into(zip_path: Path, dest: Path) -> None:
    """把 GitHub 的源码 zip 解到 dest/index-tts（zip 里顶层是 index-tts-main/）"""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        root = names[0].split("/")[0] if names else ""
        z.extractall(dest)
    top = dest / root
    if top.is_dir() and top != SRC:
        if SRC.exists():
            import shutil
            shutil.rmtree(SRC, ignore_errors=True)
        top.rename(SRC)


def install(params: dict, job: Job, report) -> dict:
    """装本地语音克隆：源码 → 运行环境（uv sync）→ 模型权重。可取消，断了能接着装。"""
    HOME.mkdir(parents=True, exist_ok=True)
    report("clone", 0, "准备本地语音克隆（IndexTTS-2.5）…")

    # 1) 源码（20 多 MB，走 GitHub 加速镜像 —— 和引擎组件同一条路）
    if not (SRC / "pyproject.toml").is_file():
        zip_path = HOME / "index-tts.zip"
        report("clone", 2, "下载安装包…")
        # validate：镜像可能用 200 回一个 HTML 错误页，不验就会拿网页去解压
        mirrors.fetch(SRC_ZIP, zip_path, params, report, stage="clone",
                      validate=zipfile.is_zipfile)
        _unzip_into(zip_path, HOME)
        zip_path.unlink(missing_ok=True)
        if not (SRC / "pyproject.toml").is_file():
            raise RuntimeError("解压后没找到 index-tts 的源码，看看日志")
    report("clone", 4, "安装包就位")

    # 2) 运行环境：uv sync（torch cu128 那一套，约 2.5 GB）
    uv = engine.find_uv() or engine.install_uv(params, job, report)
    report("clone", 6, "准备运行环境（torch 等，约 2.5 GB，第一次会久一点）…")
    # uv 自己要从 GitHub 拿 Python、从 PyPI 拿依赖：代理 / 加速前缀 / 镜像都交给 engine 那份
    _run_streamed(job, [uv, "sync"], cwd=SRC, env=engine.uv_env(params),
                  report=report, stage="clone", tag="环境")
    if not python_exe().is_file():
        raise RuntimeError("运行环境装完了但没找到 python.exe，看看日志")

    # 3) 模型权重（约 4 GB）
    spec = HOME / "download.json"
    spec.write_text(json.dumps({"repo": MODEL_REPO, "local_dir": str(CKPT),
                                "skip": list(SKIP)}, ensure_ascii=False), encoding="utf-8")
    report("clone", 70, "下载模型权重（约 4 GB，断了再点一次会接着下）…")
    env = _sub_env(params)
    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_download.py"), str(spec)]
    try:
        _run_streamed(job, cmd, cwd=SRC, env=env, report=report, stage="clone", tag="模型")
    except RuntimeError:
        # 镜像（hf_endpoint）上不一定有这个模型，直连官方源再试一次。
        # 已有下载进度会接着用（snapshot_download 认 local_dir 里的半成品）。
        job.check()
        report("clone", 70, "镜像上没找到，改直连官方源再试一次…")
        env.pop("HF_ENDPOINT", None)
        _run_streamed(job, cmd, cwd=SRC, env=env, report=report, stage="clone", tag="模型")

    st = status()
    if not st["ready"]:
        raise RuntimeError("权重下完了，但 checkpoints 里缺文件，看看日志")

    # 4) 真加载一次：装完就是「能用的状态」，别等用户真跑任务时才发现缺东西
    #    （w2v-bert / campplus / bigvgan 这几个小模型是**加载时**才补下的）
    report("clone", 92, "试跑一下，确认能加载…")
    check = HOME / "selftest.json"
    check.write_text(json.dumps({"clone_dir": str(SRC), "lang": "ZH", "bf16": True,
                                 "ref": "", "jobs": []}, ensure_ascii=False), encoding="utf-8")
    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_synth.py"), str(check)]
    try:
        _run_streamed(job, cmd, cwd=SRC, env=env, report=report, stage="clone", tag="自检")
    except RuntimeError:
        # 镜像上不一定有那几个小模型 —— 去掉镜像直连再试一次；
        # 还不行就只提示、不算安装失败（真跑的时候还会再兜一次同样的底）
        job.check()
        report("clone", 92, "镜像上没找到补下的小模型，改直连再试一次…")
        env.pop("HF_ENDPOINT", None)
        try:
            _run_streamed(job, cmd, cwd=SRC, env=env, report=report, stage="clone", tag="自检")
        except RuntimeError:
            report("clone", 96, "⚠ 试加载没成功（多半是网络），先装上；真跑的时候会自动再试一次")

    report("clone", 100, f"✅ 本地语音克隆装好了（模型 {st['model_gb']} GB）")
    return st


def synth(items, ref: Path | str, params: dict, job: Job, work: Path, progress_cb=None) -> dict:
    """整批合成（模型只加载一次）。

    items: [(文本, 输出 wav 路径, speed)]，speed 是 IndexTTS 的 duration_factor
           （1.0 = 原样，<1 更快；中文塞不进时间窗时用它，比事后变速自然）
    ref:   参考音（原说话人的一小段 wav，见 pipeline._clone_reference）
    返回 {"ok": n, "fail": n, "errors": [...]}
    """
    st = status()
    if not st["ready"]:
        raise RuntimeError("本地语音克隆还没装好（设置 → 配音 → 本地语音克隆，点「下载并启用」）")
    if not items:
        return {"ok": 0, "fail": 0, "errors": []}

    spec_path = Path(work) / "clone_jobs.json"
    spec_path.write_text(json.dumps({
        "clone_dir": str(SRC),
        "lang": "ZH",
        "bf16": True,
        "ref": str(ref),
        "jobs": [{"id": i + 1, "text": t, "out": str(o), "speed": s}
                 for i, (t, o, s) in enumerate(items)],
    }, ensure_ascii=False), encoding="utf-8")

    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_synth.py"), str(spec_path)]
    diag.write(f"[clone] synth: {len(items)} 句，参考音 {Path(ref).name}", "run")

    def _run(env: dict) -> tuple[int, int, list[str]]:
        p = job.spawn(cmd, cwd=str(SRC), env=env, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                      errors="replace", bufsize=1)
        done = ok = fail = 0
        errs: list[str] = []
        for line in p.stdout or ():
            job.check()
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("[") and "] " in line:
                done += 1
                if " fail " in line:
                    fail += 1
                    errs.append(line)
                else:
                    ok += 1
                if progress_cb:
                    progress_cb(done, len(items))
            elif line.startswith("DONE"):
                pass
            else:
                # 模型加载时的输出（一大坨），只往日志里塞，别刷界面
                diag.write(f"[clone] {line[:200]}", "run")
        code = p.wait()
        if code != 0 and ok + fail == 0:
            diag.write(f"[clone] 子进程退出码 {code}，一句都没跑成", "run")
        return ok, fail, errs

    ok, fail, errors = _run(_sub_env(params))
    if ok + fail == 0:
        # 一句都没跑成：多半是**加载模型时要补下的小模型**（w2v-bert / campplus / bigvgan）
        # 在镜像上找不到 —— 去掉镜像直连官方源再试一次（2026-10-14 实测就是栽在这儿）。
        job.check()
        diag.write("[clone] 第一遍没跑成，去掉 HF 镜像再试一次", "run")
        env2 = _sub_env(params)
        env2.pop("HF_ENDPOINT", None)
        ok, fail, errors = _run(env2)
        if ok + fail == 0:
            raise RuntimeError("本地语音克隆没能开工（模型没加载起来，详细日志见 logs 目录）")

    if fail:
        diag.write(f"[clone] {fail} 句失败：" + "；".join(errors[:3]), "run")
    return {"ok": ok, "fail": fail, "errors": errors}
