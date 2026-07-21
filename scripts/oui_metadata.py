#!/usr/bin/env python3
"""
Flock Finder — Centralized OUI Metadata
=======================================
Single source of truth for the suspected Flock Safety OUI prefixes.

The canonical data lives in ``data/flock_ouis.csv``. This module loads and
normalizes it, and can regenerate ``data/flock_ouis.json`` — a machine-readable
copy consumed by the web frontend so the OUI list is never hand-duplicated in
HTML/JS.

Run directly to (re)generate the JSON:

    python3 scripts/oui_metadata.py
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from validation import is_valid_oui, normalize_oui

SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
OUI_CSV = DATA_DIR / "flock_ouis.csv"
OUI_JSON = DATA_DIR / "flock_ouis.json"


def load_oui_metadata(csv_path: Path = None) -> list[dict]:
    """
    Load and validate OUI metadata from the canonical CSV.

    Returns a list of dicts with normalized, uppercase OUI prefixes:
        {"oui", "vendor_context", "detection_protocol", "source", "notes"}

    Rows with malformed OUIs are skipped (and reported to stderr by callers
    that care). Order is preserved from the CSV.
    """
    if csv_path is None:
        csv_path = OUI_CSV

    entries: list[dict] = []
    seen: set[str] = set()
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("oui") or "").strip()
            if not is_valid_oui(raw):
                continue
            oui = normalize_oui(raw)
            if oui in seen:
                continue  # dedupe
            seen.add(oui)
            entries.append({
                "oui": oui,
                "vendor_context": (row.get("vendor_context") or "").strip(),
                "detection_protocol": (row.get("detection_protocol") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
            })
    return entries


def write_oui_json(entries: list[dict] = None, json_path: Path = None) -> Path:
    """
    Write the centralized OUI metadata to JSON for the frontend.

    The JSON is annotated with a data-policy reminder that every match is
    only *suspected*.
    """
    if entries is None:
        entries = load_oui_metadata()
    if json_path is None:
        json_path = OUI_JSON

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "description": (
            "Canonical list of SUSPECTED Flock Safety WiFi OUI prefixes. "
            "An OUI match is a heuristic, not a confirmation."
        ),
        "match_confidence": "suspected",
        "total": len(entries),
        "ouis": entries,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    return json_path


def list_ouis(csv_path: Path = None) -> list[str]:
    """Return just the normalized OUI prefix strings."""
    return [e["oui"] for e in load_oui_metadata(csv_path)]


if __name__ == "__main__":
    entries = load_oui_metadata()
    path = write_oui_json(entries)
    print(f"[✓] Wrote {len(entries)} OUI entries → {path}")
