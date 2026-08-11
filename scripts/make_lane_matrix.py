#!/usr/bin/env python3
"""Emit a small lane matrix; each lane mirrors several shards sequentially."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--lanes", type=int, default=12)
    args = parser.parse_args()

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    try:
        config = settings["models"][args.model]
    except KeyError:
        parser.error(f"unknown model: {args.model}")
    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    weight_count = sum(item["kind"] == "weight" for item in manifest["files"])
    if args.start < 1 or args.end < args.start or args.end > weight_count:
        parser.error(f"range must be within 1..{weight_count}")
    if args.lanes < 1:
        parser.error("lanes must be positive")

    lane_count = min(args.lanes, args.end - args.start + 1)
    include = [
        {
            "model": args.model,
            "start": args.start,
            "end": args.end,
            "lane": lane,
            "lane_count": lane_count,
        }
        for lane in range(lane_count)
    ]
    print(json.dumps({"include": include}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
