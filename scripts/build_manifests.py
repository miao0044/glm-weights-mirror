#!/usr/bin/env python3
"""Build pinned, reproducible manifests from Hugging Face metadata only."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi
from huggingface_hub.hf_api import RepoFile


ROOT = Path(__file__).resolve().parents[1]


def asset_base(path: str) -> str:
    return path.replace("/", "__")


def build_one(name: str, config: dict, chunk_bytes: int, api: HfApi) -> dict:
    info = api.model_info(config["hf_repo"], revision=config["revision"])
    if info.sha != config["revision"]:
        raise RuntimeError(
            f"{name}: requested {config['revision']}, API resolved {info.sha}"
        )

    entries = list(
        api.list_repo_tree(
            config["hf_repo"],
            revision=config["revision"],
            recursive=True,
            expand=True,
        )
    )
    files = []
    weight_bytes = 0
    release_asset_count = 0

    for entry in entries:
        if not isinstance(entry, RepoFile):
            continue
        is_weight = entry.path.endswith(".safetensors")
        item = {
            "path": entry.path,
            "size": entry.size,
            "kind": "weight" if is_weight else "metadata",
            "blob_id": entry.blob_id,
        }
        if entry.lfs is not None:
            item["sha256"] = entry.lfs.sha256
        if getattr(entry, "xet_hash", None):
            item["xet_hash"] = entry.xet_hash

        if is_weight:
            part_count = math.ceil(entry.size / chunk_bytes)
            item["release_assets"] = [
                {
                    "name": f"{asset_base(entry.path)}.part{part:02d}",
                    "offset": part * chunk_bytes,
                    "size": min(chunk_bytes, entry.size - part * chunk_bytes),
                }
                for part in range(part_count)
            ]
            weight_bytes += entry.size
            release_asset_count += part_count
        files.append(item)

    files.sort(key=lambda item: item["path"])
    weight_count = sum(item["kind"] == "weight" for item in files)
    if weight_count != config["expected_weight_files"]:
        raise RuntimeError(
            f"{name}: expected {config['expected_weight_files']} weight files, got {weight_count}"
        )
    if release_asset_count >= 998:
        raise RuntimeError(
            f"{name}: {release_asset_count} weight assets leave no room in one release"
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": name,
        "source": {
            "repository": config["hf_repo"],
            "revision": config["revision"],
            "url": f"https://huggingface.co/{config['hf_repo']}/tree/{config['revision']}",
            "license": "mit",
        },
        "release_tag": config["release_tag"],
        "chunk_bytes": chunk_bytes,
        "summary": {
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "weight_file_count": weight_count,
            "weight_bytes": weight_bytes,
            "weight_release_asset_count": release_asset_count,
        },
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models", nargs="*", help="Model names; defaults to all")
    args = parser.parse_args()

    config_path = ROOT / "models.json"
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    selected = args.models or list(settings["models"])
    unknown = sorted(set(selected) - set(settings["models"]))
    if unknown:
        parser.error(f"unknown model(s): {', '.join(unknown)}")

    api = HfApi()
    for name in selected:
        model_config = settings["models"][name]
        manifest = build_one(name, model_config, settings["chunk_bytes"], api)
        output = ROOT / model_config["manifest"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary = manifest["summary"]
        print(
            f"{name}: {summary['weight_file_count']} weights, "
            f"{summary['weight_release_asset_count']} release assets, "
            f"{summary['weight_bytes']} bytes"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
