#!/usr/bin/env python3
"""Mirror one resumable lane of weight shards."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from github_assets import release_assets


ROOT = Path(__file__).resolve().parents[1]


def asset_valid(asset: dict | None, expected_size: int) -> bool:
    if asset is None:
        return False
    return (
        asset.get("state") == "uploaded"
        and asset.get("size") == expected_size
        and (asset.get("digest") or "").startswith("sha256:")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--lane", type=int, required=True, help="0-based lane number")
    parser.add_argument("--lane-count", type=int, required=True)
    args = parser.parse_args()

    if not 0 <= args.lane < args.lane_count:
        parser.error("lane must be within 0..lane-count-1")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise RuntimeError("GITHUB_REPOSITORY is required")

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    config = settings["models"][args.model]
    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    weights = [item for item in manifest["files"] if item["kind"] == "weight"]
    if args.start < 1 or args.end < args.start or args.end > len(weights):
        parser.error(f"range must be within 1..{len(weights)}")
    selected = weights[args.start - 1 : args.end][args.lane :: args.lane_count]

    print(
        f"Lane {args.lane + 1}/{args.lane_count}: {len(selected)} shards "
        f"for {args.model}",
        flush=True,
    )
    current_assets = {
        asset["name"]: asset
        for asset in release_assets(repository, config["release_tag"])
    }
    mirrored = 0
    skipped = 0

    for position, item in enumerate(selected, start=1):
        complete = all(
            asset_valid(current_assets.get(part["name"]), part["size"])
            for part in item["release_assets"]
        )
        if complete:
            skipped += 1
            print(
                f"[{position}/{len(selected)}] Already complete: {item['path']}",
                flush=True,
            )
            continue

        print(f"[{position}/{len(selected)}] Mirroring: {item['path']}", flush=True)
        env = os.environ.copy()
        env.update(
            {
                "MODEL": args.model,
                "HF_REPO": config["hf_repo"],
                "REVISION": config["revision"],
                "FILE_PATH": item["path"],
                "EXPECTED_SIZE": str(item["size"]),
                "EXPECTED_SHA256": item["sha256"],
                "EXPECTED_PART_COUNT": str(len(item["release_assets"])),
                "CHUNK_BYTES": str(settings["chunk_bytes"]),
                "RELEASE_TAG": config["release_tag"],
            }
        )
        subprocess.run(
            ["bash", str(ROOT / "scripts" / "mirror_shard.sh")],
            check=True,
            env=env,
        )
        mirrored += 1

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(
                f"\n### Lane {args.lane + 1}/{args.lane_count} complete\n"
                f"- Newly mirrored shards: `{mirrored}`\n"
                f"- Already-complete shards skipped: `{skipped}`\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
