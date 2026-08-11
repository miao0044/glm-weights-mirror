#!/usr/bin/env python3
"""Create a final marker only after every release and restored shard verifies."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from github_assets import release_assets
from verify_release import verify as verify_release
from verify_restore_lane import VERIFICATION_TAG, marker_name, valid_asset


ROOT = Path(__file__).resolve().parents[1]
FINAL_MARKER = "ALL_MODELS_RESTORE_VERIFIED.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    release_results = [
        verify_release(args.repo, name, config)
        for name, config in settings["models"].items()
    ]
    releases_complete = all(result["complete"] for result in release_results)

    assets = {
        asset["name"]: asset
        for asset in release_assets(args.repo, VERIFICATION_TAG)
    }
    expected_markers = []
    models_payload = []
    total_bytes = 0
    total_shards = 0
    for name, config in settings["models"].items():
        manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
        weights = [item for item in manifest["files"] if item["kind"] == "weight"]
        expected_markers.extend(marker_name(name, item) for item in weights)
        total_bytes += sum(item["size"] for item in weights)
        total_shards += len(weights)
        models_payload.append(
            {
                "model": name,
                "source_repository": config["hf_repo"],
                "source_revision": config["revision"],
                "weight_files": len(weights),
                "weight_bytes": sum(item["size"] for item in weights),
            }
        )

    valid_markers = sum(valid_asset(assets.get(name)) for name in expected_markers)
    status = {
        "releases_complete": releases_complete,
        "verified_shards": valid_markers,
        "expected_shards": len(expected_markers),
        "complete": releases_complete and valid_markers == len(expected_markers),
    }
    print(json.dumps(status, indent=2), flush=True)
    if not status["complete"]:
        return 0

    payload = {
        "schema_version": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "repository": args.repo,
        "verification_release": VERIFICATION_TAG,
        "method": "every Release part downloaded and checked against its GitHub SHA-256; every reconstructed weight file checked against the pinned upstream SHA-256",
        "total_weight_files": total_shards,
        "total_weight_bytes": total_bytes,
        "models": models_payload,
        "release_checks": release_results,
        "workflow_repository_sha": os.environ.get("GITHUB_SHA"),
    }
    with tempfile.TemporaryDirectory(prefix="glm-final-proof-") as temp:
        marker = Path(temp) / FINAL_MARKER
        marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                VERIFICATION_TAG,
                str(marker),
                "--repo",
                args.repo,
                "--clobber",
            ],
            check=True,
        )
    print(f"Uploaded final proof: {FINAL_MARKER}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
