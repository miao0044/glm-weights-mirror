"""Small GitHub CLI helpers shared by restore and verification scripts."""

from __future__ import annotations

import json
import subprocess


def gh_json(*args: str):
    process = subprocess.run(
        ["gh", "api", *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return json.loads(process.stdout)


def release_assets(repo: str, tag: str) -> list[dict]:
    release = gh_json(f"repos/{repo}/releases/tags/{tag}")
    release_id = release["id"]
    assets: list[dict] = []
    page = 1
    while True:
        batch = gh_json(
            "--method",
            "GET",
            f"repos/{repo}/releases/{release_id}/assets",
            "-f",
            "per_page=100",
            "-f",
            f"page={page}",
        )
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return assets
