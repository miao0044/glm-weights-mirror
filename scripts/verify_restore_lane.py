#!/usr/bin/env python3
"""Stream Release parts back, reconstruct each shard hash, and record proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from github_assets import release_assets


ROOT = Path(__file__).resolve().parents[1]
BUFFER_SIZE = 8 * 1024 * 1024
VERIFICATION_TAG = "restore-verification-v1"


def marker_name(model: str, item: dict) -> str:
    safe_path = item["path"].replace("/", "__")
    return f"{model}__{safe_path}__{item['sha256']}.verified.json"


def valid_asset(asset: dict | None, expected_size: int | None = None) -> bool:
    if asset is None:
        return False
    if asset.get("state") != "uploaded":
        return False
    if expected_size is not None and asset.get("size") != expected_size:
        return False
    return (asset.get("digest") or "").startswith("sha256:")


def stream_part(asset: dict, whole_hash) -> int:
    expected_digest = asset["digest"].removeprefix("sha256:")
    request = urllib.request.Request(
        asset["browser_download_url"],
        headers={"User-Agent": "glm-github-restore-verifier/1"},
    )
    chunk_hash = hashlib.sha256()
    byte_count = 0
    with urllib.request.urlopen(request, timeout=300) as response:
        while True:
            block = response.read(BUFFER_SIZE)
            if not block:
                break
            byte_count += len(block)
            chunk_hash.update(block)
            whole_hash.update(block)
    if byte_count != asset["size"]:
        raise IOError(
            f"downloaded size mismatch for {asset['name']}: "
            f"expected {asset['size']}, got {byte_count}"
        )
    if chunk_hash.hexdigest() != expected_digest:
        raise IOError(f"GitHub digest mismatch for {asset['name']}")
    return byte_count


def verify_shard(item: dict, assets: dict[str, dict]) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            whole_hash = hashlib.sha256()
            total_bytes = 0
            for part in item["release_assets"]:
                asset = assets.get(part["name"])
                if not valid_asset(asset, part["size"]):
                    raise RuntimeError(f"missing or invalid release asset: {part['name']}")
                print(f"    reading {part['name']}", flush=True)
                total_bytes += stream_part(asset, whole_hash)
            if total_bytes != item["size"]:
                raise IOError(
                    f"reconstructed size mismatch for {item['path']}: "
                    f"expected {item['size']}, got {total_bytes}"
                )
            actual = whole_hash.hexdigest()
            if actual != item["sha256"]:
                raise IOError(
                    f"reconstructed SHA-256 mismatch for {item['path']}: "
                    f"expected {item['sha256']}, got {actual}"
                )
            return
        except Exception as exc:  # full-shard retry boundary
            last_error = exc
            print(f"  attempt {attempt}/3 failed: {exc}", flush=True)
            if attempt < 3:
                time.sleep(attempt * 30)
    raise RuntimeError(f"unable to verify {item['path']}: {last_error}")


def upload_marker(repo: str, model: str, config: dict, item: dict) -> None:
    name = marker_name(model, item)
    payload = {
        "schema_version": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "method": "streamed GitHub Release parts; per-part GitHub SHA-256 and reconstructed whole-file SHA-256",
        "model": model,
        "source_repository": config["hf_repo"],
        "source_revision": config["revision"],
        "file": item["path"],
        "bytes": item["size"],
        "sha256": item["sha256"],
        "part_count": len(item["release_assets"]),
    }
    with tempfile.TemporaryDirectory(prefix="glm-verified-") as temp:
        marker = Path(temp) / name
        marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(1, 6):
            try:
                subprocess.run(
                    [
                        "gh",
                        "release",
                        "upload",
                        VERIFICATION_TAG,
                        str(marker),
                        "--repo",
                        repo,
                        "--clobber",
                    ],
                    check=True,
                )
                return
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(attempt * 20)
        raise RuntimeError(f"failed to upload verification marker {name}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--lane", type=int, required=True)
    parser.add_argument("--lane-count", type=int, required=True)
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is required")
    if not 0 <= args.lane < args.lane_count:
        parser.error("lane must be within 0..lane-count-1")

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    config = settings["models"][args.model]
    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    weights = [item for item in manifest["files"] if item["kind"] == "weight"]
    if args.start < 1 or args.end < args.start or args.end > len(weights):
        parser.error(f"range must be within 1..{len(weights)}")
    selected = weights[args.start - 1 : args.end][args.lane :: args.lane_count]

    model_assets = {
        asset["name"]: asset
        for asset in release_assets(repo, config["release_tag"])
    }
    verification_assets = {
        asset["name"]: asset
        for asset in release_assets(repo, VERIFICATION_TAG)
    }
    verified = 0
    skipped = 0
    print(
        f"Restore-verification lane {args.lane + 1}/{args.lane_count}: "
        f"{len(selected)} shards for {args.model}",
        flush=True,
    )

    for position, item in enumerate(selected, start=1):
        marker = marker_name(args.model, item)
        if valid_asset(verification_assets.get(marker)):
            skipped += 1
            print(f"[{position}/{len(selected)}] Already verified: {item['path']}", flush=True)
            continue
        print(f"[{position}/{len(selected)}] Reconstructing: {item['path']}", flush=True)
        verify_shard(item, model_assets)
        upload_marker(repo, args.model, config, item)
        verified += 1

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(
                f"### Restore-verification lane {args.lane + 1}/{args.lane_count}\n"
                f"- Newly verified shards: `{verified}`\n"
                f"- Previously verified shards skipped: `{skipped}`\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
