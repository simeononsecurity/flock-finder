#!/usr/bin/env python3
"""
Flock Finder — Data Integrity Validator
=======================================
Validates the committed public data artifacts. Intended to run in CI so a bad
commit can't publish malformed data.

Checks:
  * data/flock_cameras.geojson is valid GeoJSON with Point features
  * every feature has valid, in-range coordinates (FULL precision is kept —
    coordinates are never truncated so the map stays accurate)
  * data/flock_ouis.csv contains only well-formed OUIs
  * data/flock_ouis.json (if present) matches the CSV

Exit code 0 = OK, 1 = one or more problems found.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from oui_metadata import load_oui_metadata  # noqa: E402
from validation import (  # noqa: E402
    is_valid_latlon,
    is_valid_oui,
)

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"


def validate_geojson(path: Path, errors: list) -> None:
    if not path.exists():
        print(f"  [i] {path.name} not present — skipping")
        return
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        errors.append(f"{path.name}: invalid JSON ({exc})")
        return

    if data.get("type") != "FeatureCollection":
        errors.append(f"{path.name}: top-level type is not FeatureCollection")

    features = data.get("features", [])
    bad_coords = 0
    for feat in features:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        if not is_valid_latlon(lat, lon):
            bad_coords += 1

    if bad_coords:
        errors.append(f"{path.name}: {bad_coords} feature(s) with invalid coordinates")
    print(f"  [✓] {path.name}: {len(features)} features checked ({bad_coords} bad coords)")


def validate_ouis(errors: list) -> None:
    csv_path = DATA_DIR / "flock_ouis.csv"
    if not csv_path.exists():
        errors.append("flock_ouis.csv is missing")
        return
    entries = load_oui_metadata(csv_path)
    for e in entries:
        if not is_valid_oui(e["oui"]):
            errors.append(f"flock_ouis.csv: malformed OUI {e['oui']!r}")
    ouis = [e["oui"] for e in entries]
    if len(ouis) != len(set(ouis)):
        errors.append("flock_ouis.csv: duplicate OUI prefixes present")
    print(f"  [✓] flock_ouis.csv: {len(entries)} OUI entries checked")

    json_path = DATA_DIR / "flock_ouis.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text())
            json_ouis = [o["oui"] for o in payload.get("ouis", [])]
            if set(json_ouis) != set(ouis):
                errors.append("flock_ouis.json is out of sync with flock_ouis.csv "
                              "(run scripts/oui_metadata.py)")
            else:
                print(f"  [✓] flock_ouis.json: in sync ({len(json_ouis)} entries)")
        except Exception as exc:
            errors.append(f"flock_ouis.json: invalid JSON ({exc})")


def validate_stats(errors: list) -> None:
    """
    Check that scan_stats.json's counts are internally consistent.

    The dataset itself (flock_cameras.geojson / .csv / by_oui/) is published rather
    than committed, so in PR CI this file is the only artifact that still carries
    dataset-wide numbers — which makes it the right place to catch a headline that
    no longer adds up to the records behind it.
    """
    path = DATA_DIR / "scan_stats.json"
    if not path.exists():
        print("  [i] scan_stats.json not present — skipping")
        return
    try:
        stats = json.loads(path.read_text())
    except Exception as exc:
        errors.append(f"scan_stats.json: invalid JSON ({exc})")
        return

    total = stats.get("total_cameras")
    by_conf = stats.get("by_confidence")
    if not isinstance(total, int) or not isinstance(by_conf, dict):
        print("  [i] scan_stats.json: no confidence breakdown yet — skipping")
        return

    if sum(by_conf.values()) != total:
        errors.append(
            f"scan_stats.json: by_confidence sums to {sum(by_conf.values()):,} but "
            f"total_cameras is {total:,}"
        )

    actionable = stats.get("total_actionable")
    other = by_conf.get("identified_other", 0)
    if actionable is not None and actionable != total - other:
        errors.append(
            f"scan_stats.json: total_actionable {actionable:,} != total_cameras - "
            f"identified_other ({total - other:,})"
        )
    print(
        f"  [✓] scan_stats.json: {total:,} records, {actionable:,} actionable "
        f"(breakdown adds up)"
        if actionable is not None else
        f"  [✓] scan_stats.json: {total:,} records (breakdown adds up)"
    )


def main() -> int:
    print("Validating Flock Finder data artifacts…")
    errors: list = []
    validate_geojson(DATA_DIR / "flock_cameras.geojson", errors)
    for sub in sorted((DATA_DIR / "by_oui").glob("*.geojson")):
        validate_geojson(sub, errors)
    validate_ouis(errors)
    validate_stats(errors)

    if errors:
        print("\n[✗] Data validation FAILED:")
        for e in errors:
            print(f"    - {e}")
        return 1
    print("\n[✓] All data artifacts passed validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
