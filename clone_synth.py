#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在「本地语音克隆」那个独立环境里跑一批合成。

为什么要单开一个脚本、用**那个环境自己的 python** 跑：
克隆引擎的依赖（torch / transformers / 一大堆音频库）跟主程序是两套，
主程序这边 import 不了；而且模型加载一次要十几秒，一整批句子必须在同一个进程里跑完。

用法：
    <克隆目录>\\.venv\\Scripts\\python.exe clone_synth.py <jobs.json>

jobs.json 长这样：:

    {
      "clone_dir": "C:\\\\...\\\\voiceclone\\\\index-tts",
      "lang": "ZH",
      "bf16": true,
      "ref": "C:\\\\...\\\\参考音.wav",          // 原说话人的一小段（wav）
      "jobs": [{"text": "你好", "out": "C:\\\\...\\\\1.wav", "speed": 1.0}, ...]
    }

stdout 一行一条，给外面（dubber / 界面）解析：:

    [3/12] ok 1.24s
    [4/12] fail RuntimeError: ...
    DONE ok=11 fail=1
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python clone_synth.py <jobs.json>", file=sys.stderr)
        return 2

    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)

    clone_dir = spec["clone_dir"]
    # ⚠️ 必须切到克隆目录再 import：infer_v2_5.py 开头把 HF_HUB_CACHE 写成了
    #    **相对路径** './checkpoints/hf_cache'，不切目录的话那些自动补下的小模型
    #    会落到主程序的工作目录里（实测过，一堆目录凭空多出来）。
    os.chdir(clone_dir)
    sys.path.insert(0, clone_dir)

    from indextts.infer_v2_5 import IndexTTS2  # noqa: E402 - 上面得先切目录

    model_dir = os.path.join(clone_dir, "checkpoints")
    tts = IndexTTS2(cfg_path=os.path.join(model_dir, "config.yaml"),
                    model_dir=model_dir,
                    # bf16：显存省一半、速度更快，质量差别很小（官方文档口径）
                    use_bf16=bool(spec.get("bf16", True)),
                    # 不用 BigVGAN 的自定义 CUDA 核与 DeepSpeed —— 官方示例就是这么给的，
                    # 那两样要额外的编译产物，装上反而容易在别人机器上起不来
                    use_cuda_kernel=False,
                    use_deepspeed=False)

    jobs = spec.get("jobs") or []
    ref = spec["ref"]
    lang = spec.get("lang", "ZH")
    total = len(jobs)
    ok = fail = 0

    for i, j in enumerate(jobs, 1):
        job_id = j.get("id", i)
        t0 = time.time()
        try:
            tts.infer(spk_audio_prompt=ref,
                      text=j["text"],
                      output_path=j["out"],
                      lang=lang,
                      # 0.5~2.0：小于 1 说得更快。中文要塞进原来的时间窗时用它，
                      # 比事后做变速自然（韵律是模型自己安排好的）。
                      duration_factor=float(j.get("speed") or 1.0),
                      verbose=False,
                      text_normalization=True)
            ok += 1
            print(f"[{job_id}] ok {time.time() - t0:.2f}s", flush=True)
        except Exception as e:  # noqa: BLE001 - 单句失败不该拖垮整批
            fail += 1
            print(f"[{job_id}] fail {type(e).__name__}: {e}", flush=True)

    print(f"DONE ok={ok} fail={fail}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
