#!/usr/bin/env python3
"""
Flock Finder — Data Integrity Validator
=======================================
Validates the committed public data artifacts against the project's data
policy. Intended to run in CI so a bad commit can't publish malformed data
or leak full-precision coordinates.

Checks:
  * data/flock_cameras.geojson is valid GeoJSON with Point features
  * every feature has valid, in-range coordinates
  * published coordinates do not exceed PUBLIC_COORD_PRECISION decimals
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
    PUBLIC_COORD_PRECISION,
    is_valid_latlon,
    is_valid_oui,
)

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"


def _decimals(value) -> int:
    """Number of decimal places in a float's string form."""
    s = repr(float(value))
    if "e" in s or "E" in s:  # scientific notation → treat as within policy
        return 0
    return len(s.split(".")[1]) if "." in s else 0


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
    over_precision = 0
    for feat in features:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        if not is_valid_latlon(lat, lon):
            bad_coords += 1
            continue
        if _decimals(lat) > PUBLIC_COORD_PRECISION or _decimals(lon) > PUBLIC_COORD_PRECISION:
            over_precision += 1

    if bad_coords:
        errors.append(f"{path.name}: {bad_coords} feature(s) with invalid coordinates")
    if over_precision:
        errors.append(
            f"{path.name}: {over_precision} feature(s) exceed public precision "
            f"({PUBLIC_COORD_PRECISION} decimals)"
        )
    print(f"  [✓] {path.name}: {len(features)} features checked "
          f"({bad_coords} bad coords, {over_precision} over-precision)")


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


def main() -> int:
    print("Validating Flock Finder data artifacts…")
    errors: list = []
    validate_geojson(DATA_DIR / "flock_cameras.geojson", errors)
    for sub in sorted((DATA_DIR / "by_oui").glob("*.geojson")):
        validate_geojson(sub, errors)
    validate_ouis(errors)

    if errors:
        print("\n[✗] Data validation FAILED:")
        for e in errors:
            print(f"    - {e}")
        return 1
    print("\n[✓] All data artifacts passed validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
