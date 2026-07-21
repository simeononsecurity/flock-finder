#!/usr/bin/env python3
"""
Flock Finder — Reprocess Existing Data (no WiGLE query)
=======================================================
Applies the current public-data policy (coordinate precision reduction +
record validation + suspected labeling) to the ALREADY-COMMITTED dataset,
without hitting the WiGLE API.

Use this once after tightening the data policy so historical artifacts are
brought into compliance. Normal daily scans apply the policy automatically.

    python3 scripts/reprocess_data.py
"""

from __future__ import annotations

from validation import redact_coordinates, validate_record
from wigle_query import (
    DATA_DIR,
    load_existing_data,
    write_csv,
    write_csv_per_oui,
    write_geojson,
    write_geojson_per_oui,
)


def main() -> int:
    geojson_in = DATA_DIR / "flock_cameras.geojson"
    records = load_existing_data(geojson_in)
    if not records:
        print("  [!] No existing records found — nothing to reprocess.")
        return 0

    public = {}
    dropped = 0
    for netid, net in records.items():
        rec = dict(net)
        rlat, rlon = redact_coordinates(net.get("trilat"), net.get("trilong"))
        rec["trilat"] = rlat
        rec["trilong"] = rlon
        if not validate_record(rec):
            dropped += 1
            continue
        public[netid] = rec

    print(f"  Reprocessed {len(public)} records ({dropped} dropped as invalid)")

    write_geojson(public, DATA_DIR / "flock_cameras.geojson")
    write_csv(public, DATA_DIR / "flock_cameras.csv")
    write_geojson_per_oui(public, DATA_DIR)
    write_csv_per_oui(public, DATA_DIR)
    print("  [✓] All artifacts rewritten at public precision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
