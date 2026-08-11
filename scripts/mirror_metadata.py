#!/usr/bin/env python3
"""Download pinned non-weight files and package them as one release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe path: {path}")
    return candidate


def download(url: str, output: Path, expected_size: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, 7):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "glm-github-mirror/1"})
            with urllib.request.urlopen(request, timeout=180) as response, output.open("wb") as dst:
                shutil.copyfileobj(response, dst, length=8 * 1024 * 1024)
            actual_size = output.stat().st_size
            if actual_size != expected_size:
                raise IOError(f"expected {expected_size} bytes, got {actual_size}")
            return
        except Exception as exc:  # network retry boundary
            last_error = exc
            output.unlink(missing_ok=True)
            if attempt < 6:
                time.sleep(attempt * 10)
    raise RuntimeError(f"failed to download {url}: {last_error}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    settings = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    try:
        config = settings["models"][args.model]
    except KeyError:
        parser.error(f"unknown model: {args.model}")
    manifest_path = ROOT / config["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"{args.model}-metadata.tar.gz"
    with tempfile.TemporaryDirectory(prefix="glm-metadata-") as temp:
        model_root = Path(temp) / args.model
        model_root.mkdir()
        for item in manifest["files"]:
            if item["kind"] != "metadata":
                continue
            relative = safe_relative(item["path"])
            encoded = urllib.parse.quote(item["path"], safe="/")
            url = (
                f"https://huggingface.co/{config['hf_repo']}/resolve/"
                f"{config['revision']}/{encoded}?download=true"
            )
            print(f"Downloading metadata: {item['path']}", flush=True)
            download(url, model_root / relative, item["size"])
        shutil.copy2(manifest_path, model_root / "BACKUP_MANIFEST.json")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(model_root, arcname=args.model)

    manifest_asset = args.output_dir / f"{args.model}.manifest.json"
    shutil.copy2(manifest_path, manifest_asset)
    print(json.dumps({
        "archive": str(archive),
        "archive_bytes": archive.stat().st_size,
        "archive_sha256": sha256(archive),
        "manifest_asset": str(manifest_asset),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
