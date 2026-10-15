#!/usr/bin/env python3
"""Give generated Pages assets snapshot-specific paths before deployment."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SNAPSHOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
CHUNK_SIZE = 256 * 1024


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"expected one occurrence of {old!r}")
    return text.replace(old, new)


def split_archive(source: Path, target_dir: Path, chunk_size: int = CHUNK_SIZE) -> None:
    target_dir.mkdir()
    with source.open("rb") as archive:
        for index, chunk in enumerate(iter(lambda: archive.read(chunk_size), b"")):
            (target_dir / f"chunk-{index:06d}.bin").write_bytes(chunk)


def version_pages_assets(
    latest_path: Path,
    docs_dir: Path,
    site_version: str | None = None,
) -> tuple[Path, Path, Path]:
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    snapshot_id = latest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
        raise ValueError("latest pointer contains an unsafe snapshot_id")
    if site_version is not None and not SNAPSHOT_ID_PATTERN.fullmatch(site_version):
        raise ValueError("site version contains unsafe characters")

    assets_dir = docs_dir / "assets"
    tile_source = assets_dir / "flock-cameras.pmtiles"
    bssid_source = assets_dir / "bssid"
    if not tile_source.is_file():
        raise FileNotFoundError(tile_source)
    if not bssid_source.is_dir():
        raise FileNotFoundError(bssid_source)

    tile_target = assets_dir / f"flock-cameras-{snapshot_id}.pmtiles"
    chunk_target = assets_dir / f"flock-cameras-{snapshot_id}-chunks"
    bssid_target = assets_dir / f"bssid-{snapshot_id}"
    if tile_target.exists() or chunk_target.exists() or bssid_target.exists():
        raise FileExistsError("snapshot-specific Pages asset already exists")

    index_path = docs_dir / "index.html"
    index = index_path.read_text(encoding="utf-8")
    index = replace_once(
        index,
        'name="flock-tile-archive" content="assets/flock-cameras.pmtiles"',
        f'name="flock-tile-archive" content="assets/{tile_target.name}"',
    )
    index = replace_once(
        index,
        'name="flock-tile-chunks" content=""',
        f'name="flock-tile-chunks" content="assets/{chunk_target.name}"',
    )
    index = replace_once(
        index,
        'name="flock-bssid-index" content="assets/bssid"',
        f'name="flock-bssid-index" content="assets/{bssid_target.name}"',
    )
    if site_version is not None:
        index = replace_once(
            index,
            '<script src="assets/flock-map.js"></script>',
            f'<script src="assets/flock-map.js?v={site_version}"></script>',
        )

    tile_source.rename(tile_target)
    split_archive(tile_target, chunk_target)
    bssid_source.rename(bssid_target)
    index_path.write_text(index, encoding="utf-8")
    print(f"Versioned Pages assets for snapshot {snapshot_id}")
    return tile_target, chunk_target, bssid_target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, required=True)
    parser.add_argument("--site-version")
    args = parser.parse_args()
    version_pages_assets(args.latest, args.docs_dir, args.site_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
