#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把本地语音克隆要用的模型都下到 <克隆目录>/checkpoints。

在**克隆环境**里跑（用它的 huggingface_hub），这样 HF 镜像 / 代理直接吃环境变量：

    <克隆目录>\\.venv\\Scripts\\python.exe clone_download.py <spec.json>

spec.json：{"repo": "IndexTeam/IndexTTS-2.5", "local_dir": "...", "skip": ["qwen0.6bemo4-merge/*"]}

⚠️ 除了主模型，还要顺带把**三个辅助模型**下全（w2v-bert / campplus / bigvgan）：
引擎自己是**加载模型时**才去补它们，而且补完是 `local_files_only` 读本地 ——
网络一抖就只留下半份，然后每次加载都报缺文件（2026-10-14 实测栽在这）。
装的时候一次下齐，加载时就完全不碰网络了。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

# 辅助模型：仓库 → 落到 hf_cache 下的哪个位置（跟 indextts/infer_v2_5.py 里的路径对齐）
#
# ⚠️ 这两个仓库里有一大半是**训练时才用的东西**，别一起拖下来（实测多下 5 GB）：
#   w2v-bert：`conformer_shaw.pt`（2.2 G）是原版 fairseq 权重，transformers 只读
#             `model.safetensors`（2.2 G）
#   bigvgan ：`bigvgan_discriminator_optimizer*.pt`（两个 1.38 G）是训练遗留；
#             引擎只用 config 里写的 `bigvgan_generator.pt`（428 M）
AUX = [
    ("facebook/w2v-bert-2.0", "w2v-bert-2.0", ["conformer_shaw.pt"]),
    ("nvidia/bigvgan_v2_22khz_80band_256x", "bigvgan", ["*optimizer*", "*_3msteps*"]),
]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python clone_download.py <spec.json>", file=sys.stderr)
        return 2

    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)

    from huggingface_hub import hf_hub_download, snapshot_download

    local_dir = spec["local_dir"]
    path = snapshot_download(
        repo_id=spec["repo"],
        local_dir=local_dir,
        ignore_patterns=spec.get("skip") or [],
        max_workers=int(spec.get("workers") or 4),
    )
    print(f"DONE {path}", flush=True)

    cache = os.path.join(local_dir, "hf_cache")
    os.makedirs(cache, exist_ok=True)
    for repo, sub, skip in AUX:
        dest = os.path.join(cache, sub)
        # ⚠️ 别在这儿删目录：snapshot_download 认 local_dir，缺什么补什么（能断点续传）；
        #    删一次就等于把上次下好的几个 G 全扔了（2026-10-14 踩过）。
        print(f"AUX {repo} -> {dest}", flush=True)
        snapshot_download(repo_id=repo, local_dir=dest, ignore_patterns=skip, max_workers=4)
        # 顺手清掉以前多下的训练遗留（老版本没写 ignore 时下下来的）
        for junk in ("conformer_shaw.pt", "bigvgan_discriminator_optimizer.pt",
                     "bigvgan_discriminator_optimizer_3msteps.pt"):
            for root, _d, files in os.walk(dest):
                if junk in files:
                    try:
                        os.remove(os.path.join(root, junk))
                        print(f"  清掉用不到的 {junk}", flush=True)
                    except OSError:
                        pass
        print(f"AUX DONE {repo}", flush=True)

    # campplus 是单文件（加载时按固定文件名找）
    campplus = os.path.join(cache, "campplus_cn_common.bin")
    if not os.path.isfile(campplus) or os.path.getsize(campplus) < 1024:
        print("AUX funasr/campplus -> campplus_cn_common.bin", flush=True)
        got = hf_hub_download(repo_id="funasr/campplus", filename="campplus_cn_common.bin",
                              local_dir=os.path.join(cache, "_campplus"))
        shutil.copy2(got, campplus)
        print("AUX DONE funasr/campplus", flush=True)

    print("DONE AUX", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
