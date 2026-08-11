#!/usr/bin/env python3
"""Emit a bounded GitHub Actions matrix for model weight shards."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--start", type=int, required=True, help="1-based inclusive")
    parser.add_argument("--end", type=int, required=True, help="1-based inclusive")
    args = parser.parse_args()

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    try:
        config = settings["models"][args.model]
    except KeyError:
        parser.error(f"unknown model: {args.model}")

    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    weights = [item for item in manifest["files"] if item["kind"] == "weight"]
    if args.start < 1 or args.end < args.start or args.end > len(weights):
        parser.error(f"range must be within 1..{len(weights)}")
    selected = weights[args.start - 1 : args.end]
    if len(selected) > 250:
        parser.error("a single run is limited to 250 shards")

    include = []
    for item in selected:
        if "sha256" not in item:
            raise RuntimeError(f"missing upstream SHA-256 for {item['path']}")
        include.append(
            {
                "model": args.model,
                "path": item["path"],
                "size": item["size"],
                "sha256": item["sha256"],
                "part_count": len(item["release_assets"]),
                "hf_repo": config["hf_repo"],
                "revision": config["revision"],
                "release_tag": config["release_tag"],
                "chunk_bytes": settings["chunk_bytes"],
            }
        )
    print(json.dumps({"include": include}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
