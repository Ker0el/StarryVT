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
  - 8 GB 显存就能跑（bf16），12 GB 的卡绰绰有余
  - 许可：bilibili 模型使用许可（见仓库 LICENSE / LICENSE_ZH.txt）—— 免费、
    可商用（月活不过亿、年收入不过亿即可），副本要保留许可声明

**装在哪**：跟着「数据目录」走 —— 默认 `%LOCALAPPDATA%\\StarryVT\\voiceclone\\`，
在设置 → 输出 → 数据目录里可以整体换到别的盘（见 paths.py）。**绝不碰引擎那套环境**，
引擎的依赖是钉死的，混在一起早晚炸

**默认不装**：设置里点「下载并启用」才动手（环境约 2.5 GB + 模型约 4 GB）。
权重从 HuggingFace 拉，走设置里的 hf_endpoint 镜像和代理；断了再点一次会接着下。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import diag
import engine
import mirrors
import paths
import tools
from job import Job


def home() -> Path:
    """克隆自己的根目录（跟着设置里的数据目录走，可换盘）"""
    return paths.resolve("voiceclone")


def src() -> Path:
    """源码 + .venv 都在这儿"""
    return home() / "index-tts"


def ckpt() -> Path:
    """模型权重"""
    return src() / "checkpoints"
# 两个辅助脚本跟主程序一起走（build_nuitka.py 的 --include-data-file 和
# _rebuild_backend.py 的 --add-data 里都列了它们；那份 StarryVT.spec 早就没人用了）。
APP_DIR = (Path(getattr(sys, "_MEIPASS", tools.exec_path().parent))
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

#: 本机克隆至少要的显存（IndexTTS-2.5 用 bf16 跑；见文件头「8 GB 显存就能跑」）
MIN_VRAM_GB = 8.0

# 加载模型时**真正会读**的文件（infer_v2_5.py + checkpoints/config.yaml）：
#   config.yaml / gpt.pth / s2mel.pth / codec.pth / feat1.pt / feat2.pt /
#   wav2vec2bert_stats.pt / 分词器 tiktoken
# 少任何一个都会在构造函数里直接抛，不会自己补，所以 ready 必须按这张表判定。
# 2026-10-26 客户：下载在 s2mel.pth 处断了（SSL 中断），config.yaml + gpt.pth 已落盘；
# 旧判定只查这两个，把半成品报成「已装好」——界面藏了下载按钮、任务也不走云端兜底，
# 用户连点三天空闲都失败，日志里只有 FileNotFoundError: …\s2mel.pth。
MODEL_FILES = (
    "config.yaml",
    "gpt.pth",
    "s2mel.pth",
    "codec.pth",
    "feat1.pt",
    "feat2.pt",
    "wav2vec2bert_stats.pt",
    "multilingual_zh_ja_yue_char_del.tiktoken",
)


def missing_model_files() -> list[str]:
    """checkpoints 里缺的 / 空掉的文件名（顺序跟 MODEL_FILES 一致）"""
    c = ckpt()
    miss: list[str] = []
    for name in MODEL_FILES:
        p = c / name
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                miss.append(name)
        except OSError:
            miss.append(name)
    return miss


def _short(names: list[str], limit: int = 3) -> str:
    text = "、".join(names[:limit])
    return text + (f" 等 {len(names)} 个" if len(names) > limit else "")


def python_exe() -> Path:
    """克隆环境自己的 python（跟主程序那套是两个世界）"""
    return src() / ".venv" / "Scripts" / "python.exe"


def venv_broken(code: int, last_line: str = "") -> bool:
    """克隆环境的 Python 启动器是不是坏了（「No Python at …」/ 退出码 103）

    ★ 2026-11-05 客户报告（`E:\\星译\\StarryVT\\...`）：启动器按系统代码页解析
      pyvenv.cfg 里的 `home =`，`星译` 变成 `??`，只吐一行
      `No Python at '"E:\\??\\StarryVT\\...\\python.exe'`、退出码 103。
      数据目录改名成英文是最好的，但用户不该自己去搬 15 GB —— 这里自动重建。
    """
    low = (last_line or "").lower()
    return code == 103 or "no python at" in low or not python_exe().is_file()


def repair_venv(params: dict, job: Job) -> bool:
    """删掉 `.venv` 重新 `uv sync`（启动器找不到解释器时）。返回是否修好。"""
    s = src()
    uv = engine.find_uv()
    if not uv:
        diag.write("[clone] 想重建运行环境，但找不到 uv", "run")
        return False
    diag.write("[clone] 运行环境的 Python 启动器坏了，重建 .venv…", "run")
    shutil.rmtree(s / ".venv", ignore_errors=True)
    try:
        _run_streamed(job, [uv, "sync"], cwd=s, env=engine.uv_env(params),
                      report=lambda stage, _pct, msg: diag.write(f"[clone] {msg}"[:220], "run"),
                      stage="clone", tag="环境")
    except Exception as e:  # noqa: BLE001 - 修不好只影响这次，别再往上抛
        diag.write(f"[clone] 重建 .venv 失败：{e}"[:220], "run")
        return False
    ok = python_exe().is_file()
    diag.write("[clone] 运行环境重建完成" if ok else "[clone] 重建后仍然没有 python.exe", "run")
    return ok


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _torch_ready() -> bool:
    """环境里**真的有 torch** 吗

    ★ 2026-11-11 客户：`uv sync` 下 torch 失败（退出码 2），但 `.venv\\Scripts\\
      python.exe` 已经建出来了 —— 只看 python.exe 的旧判定把「运行环境装好了」报给
      用户，模型那一半还空着，人以为只差模型。
    """
    sp = src() / ".venv" / "Lib" / "site-packages"
    return (sp / "torch" / "__init__.py").is_file()


class Unsupported(RuntimeError):
    """这台机器跑不了本机克隆（显存不够）—— 这是「跳过」，不是「装坏了」。

    一键初始化会把它当成可跳过的项：不下载、不报红，只在日志里说明改用云端克隆。
    """


def blocked_reason() -> str:
    """这台机器为什么跑不了本机克隆；空串 = 能跑（或查不到显卡，不拦）。

    ★ 2026-11-11 客户（GTX 960 4 GB）：界面只提示「约 1.5 倍」，他反复下 15 GB
      都失败。显存不够的一律挡在下载之前 —— 装上也只会退回 CPU 跑几小时。
    """
    try:
        import doctor
        g = doctor.gpu() or {}
    except Exception:  # noqa: BLE001 - 查不到显卡就别拦
        return ""
    vram = float(g.get("vram_gb") or 0)
    if g.get("name") and 0 < vram < MIN_VRAM_GB:
        return (f"本机语音克隆至少要 {MIN_VRAM_GB:g} GB 显存（IndexTTS-2.5 用 bf16 跑），"
                f"这台机器的 {g['name']} 只有 {vram:g} GB；装上也只会退回 CPU 跑"
                f"（长片要几个小时）。请改用「云端克隆」。")
    return ""


def status() -> dict:
    """装到哪一步了：{ready, installed, model_ready, missing, problem, dir, model_gb, detail}"""
    h, s, c = home(), src(), ckpt()
    env_ok = python_exe().is_file() and _torch_ready()
    miss = missing_model_files()
    model_ok = not miss
    gb = round(_dir_size(c) / 1024 ** 3, 1) if c.is_dir() else 0.0

    if env_ok and model_ok:
        detail = (f"已装好（模型 {gb} GB），随时可用\n位置：{h}" if gb
                  else f"已装好，随时可用\n位置：{h}")
        problem = ""
    elif env_ok:
        problem = f"模型没下完（缺 {_short(miss)}）" if miss else "模型没下完"
        if c.is_dir() and len(miss) < len(MODEL_FILES):
            detail = (f"运行环境装好了，{problem}\n"
                      f"点右边的按钮接着下，已下好的不会重下\n位置：{h}")
        else:
            detail = f"运行环境装好了，模型还没下\n位置：{h}"
    elif s.is_dir():
        detail = f"上次没装完，点右边的按钮接着装\n位置：{h}"
        problem = "上次没装完"
    else:
        # 实测：运行环境 8.2 GB（torch+CUDA 那一套）+ 模型 ~6.8 GB
        detail = (f"没装（约 15 GB：运行环境 8 G + 模型 7 G）\n将装在：{h}")
        problem = "还没安装"

    return {"ready": env_ok and model_ok, "installed": env_ok, "model_ready": model_ok,
            "missing": miss, "problem": problem, "dir": str(h), "model_gb": gb,
            "detail": detail}


def speed_hint(has_nvidia: bool, vram_gb: float = 0.0) -> str:
    """这台机器上本机克隆会不会明显更慢（给设置页直接显示）。

    克隆环境是 cu128 的 torch，只有 NVIDIA 卡能上 GPU；AMD / 核显机器上
    torch 在 Windows 只能走 CPU，同一段素材会慢 5～10 倍。不是故障，但用户
    选之前应该知道 —— 2026-10-28 实测：配音耗时约为视频时长的 1.5 倍。

    ★ 2026-11-11 客户：GTX 960 4 GB。以前只要有 NVIDIA 就报「约 1.5 倍」，
      4 GB 的卡根本跑不动（见文件头的 8 GB 要求），等于把人骗去下 15 GB。
    """
    if has_nvidia and (not vram_gb or vram_gb >= MIN_VRAM_GB):
        return "本机克隆的配音时间大约是视频时长的 1.5 倍，显卡越弱越慢。"
    if has_nvidia:
        return (f"这块显卡只有 {vram_gb:g} GB 显存，本机克隆至少要 {MIN_VRAM_GB:g} GB，"
                "装上也只会退回 CPU 跑（长片要几个小时）。建议改用云端克隆。")
    return ("这台机器没检测到 NVIDIA 显卡，本机克隆只能用 CPU 跑，视频越长越慢"
            "（长片可能要几个小时）。建议改用云端克隆。")


def _sub_env(params: dict) -> dict:
    """跑子进程用的环境：代理 + HF 镜像 + 强制 UTF-8 输出"""
    env = dict(os.environ)
    proxy = mirrors.effective_proxy(params or {})
    if proxy:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    else:
        # 没有可用代理：清掉继承来的代理变量并强制直连，免得子进程去读那个已经关掉的系统代理
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = env["no_proxy"] = "*"
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
    target = dest / "index-tts"
    if top.is_dir() and top != target:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        top.rename(target)


def install(params: dict, job: Job, report) -> dict:
    """装本地语音克隆：源码 → 运行环境（uv sync）→ 模型权重。可取消，断了能接着装。"""
    h, s, c = home(), src(), ckpt()
    h.mkdir(parents=True, exist_ok=True)
    # 运行环境约 8 GB + 模型约 7 GB。空间不够就别开始下，不然下到一半把盘写满，
    # 用户看到的只是一句莫名其妙的写入失败（2026-10-19 全面检查时补的）。
    try:
        free_gb = shutil.disk_usage(h.anchor or str(h)).free / 1024 ** 3
    except OSError:
        free_gb = None
    if free_gb is not None and free_gb < 16:
        raise RuntimeError(
            f"本地语音克隆需要约 15 GB，{h.anchor} 只剩 {free_gb:.1f} GB。"
            f"请换一个空间足够的「数据目录」（设置 → 输出 → 数据目录），或改用云端配音。")
    # ★ 2026-11-11：显存不够就别开始下这 15 GB。客户那台是 GTX 960 4 GB，
    #   界面只提示「约 1.5 倍」，他反复下 torch（2.6 GB）都失败，白耗两天。
    why = blocked_reason()
    if why:
        raise Unsupported(why)
    report("clone", 0, "准备本地语音克隆（IndexTTS-2.5）…")
    if not str(h).isascii():
        # 文本前端 wetext 用 C++ 的 kaldifst 打开 .fst，按系统代码页解析路径，
        # 中文目录会报「Error opening input stream」。clone_synth.py 会自动把
        # wetext 复制到纯英文目录再加载；这里仍然提醒一句，避免换到别处又踩坑。
        report("clone", 1, "提示：安装路径含中文，已自动用英文临时目录加载文本前端；"
                           "如果仍报 Path/OSError，把数据目录换到纯英文路径（如 D:\\StarryVT）")

    # 1) 源码（20 多 MB，走 GitHub 加速镜像 —— 和引擎组件同一条路）
    if not (s / "pyproject.toml").is_file():
        zip_path = h / "index-tts.zip"
        report("clone", 2, "下载安装包…")
        # validate：镜像可能用 200 回一个 HTML 错误页，不验就会拿网页去解压
        mirrors.fetch(SRC_ZIP, zip_path, params, report, stage="clone",
                      validate=zipfile.is_zipfile)
        _unzip_into(zip_path, h)
        zip_path.unlink(missing_ok=True)
        if not (s / "pyproject.toml").is_file():
            raise RuntimeError("解压后没找到 index-tts 的源码，看看日志")
    report("clone", 4, "安装包就位")

    # 2) 运行环境：uv sync（torch cu128 那一套，约 2.5 GB）
    uv = engine.find_uv() or engine.install_uv(params, job, report)
    report("clone", 6, "准备运行环境（torch 等，约 2.5 GB，第一次会久一点）…")
    # uv 自己要从 GitHub 拿 Python、从 PyPI 拿依赖：代理 / 加速前缀 / 镜像都交给 engine 那份
    _run_streamed(job, [uv, "sync"], cwd=s, env=engine.uv_env(params),
                  report=report, stage="clone", tag="环境")
    if not python_exe().is_file():
        # 2026-11-05：中文数据目录下启动器会吐「No Python at …」，uv sync 却返回 0
        report("clone", 6, "运行环境的 Python 启动器没建好，重建一次…")
        if not repair_venv(params, job):
            raise RuntimeError("运行环境装完了但没找到 python.exe，看看日志")

    # 3) 模型权重（约 4 GB）
    spec = h / "download.json"
    env = _sub_env(params)
    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_download.py"), str(spec)]
    miss0 = missing_model_files()
    if 0 < len(miss0) < len(MODEL_FILES):
        # 上次下到一半（客户那台就是断了 s2mel.pth）：接着下，别让人以为要重来
        report("clone", 70,
               f"继续下载模型（还缺 {_short(miss0)}；已下好的不会重下）…")
    else:
        report("clone", 70, "下载模型权重（约 4 GB，断了再点一次会接着下）…")

    # 下载源按顺序试：① 设置里的 HF 镜像（有的话） ② HF 官方直连 ③ ModelScope。
    # 2026-10-26 客户那台：HF 大文件走到 s2mel.pth 就 SSL 断，而 ModelScope 很稳
    # （引擎自动补 w2v-bert 时 2 GB 全速下完）。clone_download 按 spec 的 source 走。
    plans: list[tuple[str, dict]] = [("hf", env)]
    env_direct = dict(env)
    if (env_direct.pop("HF_ENDPOINT", "") or "").strip():
        plans.append(("hf", env_direct))
    plans.append(("modelscope", env_direct))
    last_err: RuntimeError | None = None
    for i, (source, env_try) in enumerate(plans):
        spec.write_text(json.dumps({"repo": MODEL_REPO, "local_dir": str(c),
                                    "skip": list(SKIP), "source": source},
                                   ensure_ascii=False), encoding="utf-8")
        if i:
            job.check()
            report("clone", 70,
                   "HF 上没下完，改从 ModelScope 再试一次…" if source == "modelscope"
                   else "镜像上没找到，改直连官方源再试一次…")
        try:
            _run_streamed(job, cmd, cwd=s, env=env_try,
                          report=report, stage="clone", tag="模型")
            last_err = None
            break
        except RuntimeError as e:
            last_err = e
    if last_err is not None:
        raise last_err

    miss = missing_model_files()
    if miss:
        raise RuntimeError(f"权重下完了，但 checkpoints 里还缺 {_short(miss)}，看看日志")

    # 4) 真加载一次：装完就是「能用的状态」，别等用户真跑任务时才发现缺东西
    #    （w2v-bert / campplus / bigvgan 这几个小模型是**加载时**才补下的）
    report("clone", 92, "试跑一下，确认能加载…")
    check = h / "selftest.json"
    check.write_text(json.dumps({"clone_dir": str(s), "lang": "ZH", "bf16": True,
                                 "ref": "", "jobs": []}, ensure_ascii=False), encoding="utf-8")
    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_synth.py"), str(check)]
    try:
        _run_streamed(job, cmd, cwd=s, env=env, report=report, stage="clone", tag="自检")
    except RuntimeError:
        # 镜像上不一定有那几个小模型 —— 去掉镜像直连再试一次；
        # 还不行就只提示、不算安装失败（真跑的时候还会再兜一次同样的底）
        job.check()
        report("clone", 92, "镜像上没找到补下的小模型，改直连再试一次…")
        env.pop("HF_ENDPOINT", None)
        try:
            _run_streamed(job, cmd, cwd=s, env=env, report=report, stage="clone", tag="自检")
        except RuntimeError:
            report("clone", 96, "⚠ 试加载没成功（多半是网络），先装上；真跑的时候会自动再试一次")

    st = status()
    report("clone", 100, f"✅ 本地语音克隆装好了（模型 {st['model_gb']} GB）")
    return st


#: 子进程的进度行只有这一种形状：`[3] ok 1.24s` / `[4] fail RuntimeError: ...`（也容忍 [3/12]）
#: ⚠ 以前只判「行首 [ 且中间有 ] 」，kaldifst 的 `[E] Error opening input stream ...`
#:   被当成一句合成成功（2026-11-03 客户报告：界面显示 1/168，随后 ffmpeg 报找不到 wav）。
_STATUS_RE = re.compile(r"^\[\d+(?:/\d+)?\] (?:ok|fail)\b")


def synth(items, ref: Path | str, params: dict, job: Job, work: Path, progress_cb=None) -> dict:
    # ⚠️ 2026-10-28 实测过「两个进程各跑一半」：12 GB 的卡上单进程峰值
    #    9.85 GB，两个进程同时加载后显存 11.7/12.3 GB、GPU 100%，6 分钟一句都
    #    没合成出来（互相抢显存）。所以这里保持单进程串行 —— 想真并行至少要
    #    24 GB 的卡，不值当为极少数机器加一条分支。
    """整批合成（模型只加载一次）。

    items: [(文本, 输出 wav 路径, speed)]，speed 是 IndexTTS 的 duration_factor
           （1.0 = 原样，<1 更快；中文塞不进时间窗时用它，比事后变速自然）
    ref:   参考音（原说话人的一小段 wav，见 pipeline._clone_reference）
    返回 {"ok": n, "fail": n, "errors": [...]}
    """
    st = status()
    if not st["ready"]:
        miss = st.get("missing") or []
        if st.get("installed") and miss:
            raise RuntimeError(
                f"本地语音克隆的模型没下完（缺 {_short(miss)}），"
                "请到 设置 → 配音 → 本地语音克隆，点右边的按钮接着下")
        raise RuntimeError("本地语音克隆还没装好（设置 → 配音 → 本地语音克隆，点「下载并启用」）")
    if not items:
        return {"ok": 0, "fail": 0, "errors": []}

    spec_path = Path(work) / "clone_jobs.json"
    s = src()
    spec_path.write_text(json.dumps({
        "clone_dir": str(s),
        "lang": "ZH",
        "bf16": True,
        "ref": str(ref),
        "jobs": [{"id": i + 1, "text": t, "out": str(o), "speed": s}
                 for i, (t, o, s) in enumerate(items)],
    }, ensure_ascii=False), encoding="utf-8")

    cmd = [str(python_exe()), str(SCRIPT_DIR / "clone_synth.py"), str(spec_path)]
    diag.write(f"[clone] synth: {len(items)} 句，参考音 {Path(ref).name}", "run")

    def _run(env: dict) -> tuple[int, int, list[str], str]:
        p = job.spawn(cmd, cwd=str(s), env=env, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                      errors="replace", bufsize=1)
        done = ok = fail = 0
        errs: list[str] = []
        tail: list[str] = []
        for line in p.stdout or ():
            job.check()
            line = line.rstrip()
            if not line:
                continue
            if _STATUS_RE.match(line):
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
                # 模型加载时的输出（一大坨），只往日志里塞，别刷界面；
                # 最后一行留作「一句都没跑成」时的原因（客户日志里就是那句
                # FileNotFoundError: …\s2mel.pth，以前被吞掉了）
                diag.write(f"[clone] {line[:200]}", "run")
                tail.append(line)
                tail = tail[-4:]
        code = p.wait()
        if code != 0 and ok + fail == 0:
            diag.write(f"[clone] 子进程退出码 {code}，一句都没跑成", "run")
        return ok, fail, errs, (tail[-1] if tail else ""), code

    if not python_exe().is_file():
        # 环境整个不在了（清理软件删了 / 中文目录下没建好）：先修再跑，
        # 否则 Popen 直接抛一个看不懂的 FileNotFoundError（2026-11-05）
        job.check()
        repair_venv(params, job)
    ok, fail, errors, last_line, code = _run(_sub_env(params))
    if ok + fail == 0 and venv_broken(code, last_line):
        # 启动器找不到解释器（No Python at …／退出码 103）：重建 .venv 再跑一次。
        # 这类失败和网络无关，去掉镜像重试也没用（2026-11-05 客户报告）。
        job.check()
        if repair_venv(params, job):
            ok, fail, errors, last_line, code = _run(_sub_env(params))
    if ok + fail == 0:
        # 一句都没跑成：多半是**加载模型时要补下的小模型**（w2v-bert / campplus / bigvgan）
        # 在镜像上找不到 —— 去掉镜像直连官方源再试一次（2026-10-14 实测就是栽在这儿）。
        job.check()
        diag.write("[clone] 第一遍没跑成，去掉 HF 镜像再试一次", "run")
        env2 = _sub_env(params)
        env2.pop("HF_ENDPOINT", None)
        ok, fail, errors, last_line, code = _run(env2)
        if ok + fail == 0:
            why = f"：{last_line[:200]}" if last_line else ""
            raise RuntimeError(
                f"本地语音克隆没能开工（模型没加载起来{why}；详细日志见 logs 目录）")

    # 子进程说「跑完了」不算数：以音频真的落盘为准。少了任何一句，装配阶段就会
    # 报一句看不懂的「ffmpeg 找不到 wav」，整条任务白跑（2026-11-03 客户报告）。
    missing = []
    for _text, _out, _speed in items:
        _p = Path(_out)
        try:
            if not _p.is_file() or _p.stat().st_size <= 500:
                missing.append(_p.name)
        except OSError:
            missing.append(_p.name)
    if missing:
        diag.write(f"[clone] {len(missing)} 句没有生成音频（如 {missing[0]}）", "run")
        ok = max(0, len(items) - len(missing))
        fail = len(missing)
        errors = [f"{len(missing)} 句没有生成音频（如 {missing[0]}）"] + errors

    if fail:
        diag.write(f"[clone] {fail} 句失败：" + "；".join(errors[:3]), "run")
    return {"ok": ok, "fail": fail, "errors": errors}
