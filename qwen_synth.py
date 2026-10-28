#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在 Qwen3-TTS 自己的运行环境里跑一批合成。

用法：
    <qwen 运行时>\Scripts\python.exe qwen_synth.py <jobs.json>

jobs.json：
    {
      "model_dir": "...\\Qwen3-TTS-0.6B-Base",       // 克隆用
      "custom_dir": "...\\Qwen3-TTS-0.6B-CustomVoice", // 预设音色用（可空）
      "ref": "参考音.wav", "ref_text": "",            // 空 = 仅用 x-vector
      "device": "cuda", "batch": 6,
      "jobs": [{"text": "...", "out": "...", "speaker": ""}, ...]
    }

stdout 每行一条，外面解析：
    [3] ok 1.24s
    DONE ok=11 fail=1
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python qwen_synth.py <jobs.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    device = spec.get("device") or ("cuda:0" if torch.cuda.is_available() else "cpu")
    dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    batch = max(1, int(spec.get("batch") or 6))
    jobs = spec.get("jobs") or []
    preset = any(j.get("speaker") for j in jobs)
    model_dir = spec.get("custom_dir") if preset else spec.get("model_dir")
    if not model_dir or not os.path.isdir(model_dir):
        print(f"模型目录不存在: {model_dir}", file=sys.stderr)
        return 2

    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(model_dir, device_map=device, dtype=dtype)

    prompt = None
    if not preset:
        ref = spec.get("ref") or ""
        ref_text = spec.get("ref_text") or ""
        if not ref or not os.path.isfile(ref):
            print(f"参考音不存在: {ref}", file=sys.stderr)
            return 2
        # 参考音只编码一次，之后每批复用
        prompt = model.create_voice_clone_prompt(
            ref_audio=ref, ref_text=ref_text or None,
            x_vector_only_mode=not bool(ref_text))
    print(f"loaded {time.time()-t0:.1f}s device={device}", flush=True)

    ok = fail = 0
    for start in range(0, len(jobs), batch):
        chunk = jobs[start:start + batch]
        texts = [j["text"] for j in chunk]
        t0 = time.time()
        try:
            if preset:
                speakers = [j.get("speaker") for j in chunk]
                wavs, sr = model.generate_custom_voice(
                    text=texts, speaker=speakers, language="Chinese")
            else:
                wavs, sr = model.generate_voice_clone(
                    text=texts, language="Chinese", voice_clone_prompt=prompt)
            for j, w in zip(chunk, wavs):
                sf.write(j["out"], w, sr)
                ok += 1
            print(f"[{start + len(chunk)}] ok {time.time()-t0:.2f}s", flush=True)
        except Exception as e:  # noqa: BLE001 - 一批失败不拖垮整批任务
            fail += len(chunk)
            print(f"[{start + len(chunk)}] fail {type(e).__name__}: {e}", flush=True)
    print(f"DONE ok={ok} fail={fail}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
