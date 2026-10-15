import json
from pathlib import Path

import pytest

from scripts.version_pages_assets import version_pages_assets


def make_pages_tree(tmp_path: Path, snapshot_id: str = "20261014T060629Z-3cc05e523898") -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    assets = docs / "assets"
    (assets / "bssid").mkdir(parents=True)
    (assets / "flock-cameras.pmtiles").write_bytes(b"PMTiles")
    (assets / "bssid" / "00.json").write_text("{}", encoding="utf-8")
    (docs / "index.html").write_text(
        '<meta name="flock-tile-archive" content="assets/flock-cameras.pmtiles">\n'
        '<meta name="flock-bssid-index" content="assets/bssid">\n',
        encoding="utf-8",
    )
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8")
    return latest, docs


def test_versions_pages_assets_and_updates_index(tmp_path: Path) -> None:
    latest, docs = make_pages_tree(tmp_path)

    tile, bssid = version_pages_assets(latest, docs)

    assert tile.name == "flock-cameras-20261014T060629Z-3cc05e523898.pmtiles"
    assert tile.read_bytes() == b"PMTiles"
    assert (bssid / "00.json").read_text(encoding="utf-8") == "{}"
    index = (docs / "index.html").read_text(encoding="utf-8")
    assert tile.name in index
    assert bssid.name in index
    assert not (docs / "assets" / "flock-cameras.pmtiles").exists()
    assert not (docs / "assets" / "bssid").exists()


def test_rejects_unsafe_snapshot_id(tmp_path: Path) -> None:
    latest, docs = make_pages_tree(tmp_path, "../unsafe")

    with pytest.raises(ValueError, match="unsafe snapshot_id"):
        version_pages_assets(latest, docs)
