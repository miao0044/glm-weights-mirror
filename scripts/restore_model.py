#!/usr/bin/env python3
"""Restore one model from chunked GitHub Release assets with verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from github_assets import release_assets


ROOT = Path(__file__).resolve().parents[1]
BUFFER_SIZE = 8 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(BUFFER_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def download_asset(repo: str, asset: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/releases/assets/{asset['id']}",
                "-H",
                "Accept: application/octet-stream",
            ],
            check=True,
            stdout=output,
        )
    if destination.stat().st_size != asset["size"]:
        raise IOError(f"size mismatch for {asset['name']}")
    expected = (asset.get("digest") or "").removeprefix("sha256:")
    if not expected:
        raise IOError(f"GitHub did not report a digest for {asset['name']}")
    actual = sha256_file(destination)
    if actual != expected:
        raise IOError(f"GitHub digest mismatch for {asset['name']}")


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            resolved = (destination / member.name).resolve()
            if destination not in resolved.parents and resolved != destination:
                raise ValueError(f"unsafe archive member: {member.name}")
        tar.extractall(destination, filter="data")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="miao0044/glm-weights-mirror")
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True, type=Path, help="Parent output directory")
    parser.add_argument("--skip-metadata", action="store_true")
    args = parser.parse_args()

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    try:
        config = settings["models"][args.model]
    except KeyError:
        parser.error(f"unknown model: {args.model}")
    manifest = json.loads((ROOT / config["manifest"]).read_text(encoding="utf-8"))
    assets = {asset["name"]: asset for asset in release_assets(args.repo, config["release_tag"])}
    args.output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="glm-restore-") as temp_name:
        temp = Path(temp_name)
        if not args.skip_metadata:
            metadata_name = f"{args.model}-metadata.tar.gz"
            metadata = assets.get(metadata_name)
            if metadata is None:
                raise RuntimeError(f"missing release asset: {metadata_name}")
            archive = temp / metadata_name
            print(f"Downloading {metadata_name}")
            download_asset(args.repo, metadata, archive)
            safe_extract(archive, args.output)

        model_root = args.output / args.model
        model_root.mkdir(parents=True, exist_ok=True)
        weights = [item for item in manifest["files"] if item["kind"] == "weight"]
        for number, item in enumerate(weights, start=1):
            target = model_root / item["path"]
            if target.exists() and target.stat().st_size == item["size"]:
                print(f"[{number}/{len(weights)}] Hashing existing {item['path']}")
                if sha256_file(target) == item["sha256"]:
                    print("  already verified")
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            partial.unlink(missing_ok=True)
            whole_hash = hashlib.sha256()
            print(f"[{number}/{len(weights)}] Restoring {item['path']}")
            with partial.open("wb") as output:
                for part in item["release_assets"]:
                    asset = assets.get(part["name"])
                    if asset is None:
                        raise RuntimeError(f"missing release asset: {part['name']}")
                    if asset["size"] != part["size"]:
                        raise RuntimeError(f"release asset size mismatch: {part['name']}")
                    chunk = temp / part["name"]
                    print(f"  downloading {part['name']}")
                    download_asset(args.repo, asset, chunk)
                    with chunk.open("rb") as src:
                        for block in iter(lambda: src.read(BUFFER_SIZE), b""):
                            output.write(block)
                            whole_hash.update(block)
                    chunk.unlink()
            if partial.stat().st_size != item["size"]:
                raise IOError(f"restored size mismatch for {item['path']}")
            if whole_hash.hexdigest() != item["sha256"]:
                raise IOError(f"restored SHA-256 mismatch for {item['path']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial.replace(target)

    print(f"Restored and verified {args.model} at {args.output / args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
