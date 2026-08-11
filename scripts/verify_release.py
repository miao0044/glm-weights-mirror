#!/usr/bin/env python3
"""Verify release asset names, sizes, states, and GitHub SHA-256 presence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from github_assets import release_assets


ROOT = Path(__file__).resolve().parents[1]


def verify(repo: str, name: str, config: dict) -> dict:
    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    actual = {asset["name"]: asset for asset in release_assets(repo, config["release_tag"])}
    missing = []
    bad = []
    expected_bytes = 0
    expected_count = 0
    present_valid_bytes = 0

    for item in manifest["files"]:
        if item["kind"] != "weight":
            continue
        for part in item["release_assets"]:
            expected_count += 1
            expected_bytes += part["size"]
            asset = actual.get(part["name"])
            if asset is None:
                missing.append(part["name"])
                continue
            digest = asset.get("digest") or ""
            if (
                asset.get("state") != "uploaded"
                or asset.get("size") != part["size"]
                or not digest.startswith("sha256:")
            ):
                bad.append(
                    {
                        "name": part["name"],
                        "expected_size": part["size"],
                        "actual_size": asset.get("size"),
                        "state": asset.get("state"),
                        "digest": digest,
                    }
                )
            else:
                present_valid_bytes += part["size"]

    metadata_names = {
        f"{name}-metadata.tar.gz",
        f"{name}.manifest.json",
    }
    missing_metadata = sorted(metadata_names - set(actual))
    complete = not missing and not bad and not missing_metadata
    return {
        "model": name,
        "release_tag": config["release_tag"],
        "complete": complete,
        "expected_weight_assets": expected_count,
        "present_valid_weight_assets": expected_count - len(missing) - len(bad),
        "expected_weight_bytes": expected_bytes,
        "present_valid_weight_bytes": present_valid_bytes,
        "percent_by_bytes": round(100 * present_valid_bytes / expected_bytes, 3),
        "missing_count": len(missing),
        "bad_count": len(bad),
        "missing_metadata": missing_metadata,
        "missing_sample": missing[:10],
        "bad_sample": bad[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--model", action="append", help="Defaults to all models")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Report progress without a nonzero exit status",
    )
    args = parser.parse_args()
    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    selected = args.model or list(settings["models"])
    results = [verify(args.repo, name, settings["models"][name]) for name in selected]
    print(json.dumps(results, indent=2))
    return 0 if args.allow_incomplete or all(result["complete"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
