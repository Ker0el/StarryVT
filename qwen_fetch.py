#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在 Qwen 运行时里下载两个 0.6B 模型（ModelScope，国内直连）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python qwen_fetch.py <spec.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    from modelscope import snapshot_download

    for repo, dest in spec["repos"]:
        print(f"下载 {repo} -> {dest}", flush=True)
        snapshot_download(repo, local_dir=dest, max_workers=4)
        print(f"完成 {repo}", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
