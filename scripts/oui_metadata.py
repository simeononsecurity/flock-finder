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

from validation import (
    CONFIDENCE_ACTIONABLE,
    CONFIDENCE_DESCRIPTIONS,
    CONFIDENCE_LEVELS,
    OUI_TIER_DEFAULT,
    OUI_TIERS,
    PRIMARY_MARKET_COUNTRIES,
    SSID_CONFIRM_PATTERNS,
    SSID_DENYLIST_NOTES,
    SSID_DENYLIST_PATTERNS,
    SSID_DENYLIST_RULES,
    is_valid_oui,
    is_valid_tier,
    normalize_oui,
)

SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
OUI_CSV = DATA_DIR / "flock_ouis.csv"
OUI_JSON = DATA_DIR / "flock_ouis.json"


def load_oui_metadata(csv_path: Path = None) -> list[dict]:
    """
    Load and validate OUI metadata from the canonical CSV.

    Returns a list of dicts with normalized, uppercase OUI prefixes:
        {"oui", "vendor_context", "detection_protocol", "source", "notes", "tier"}

    `tier` is the confidence tier of the prefix ('high' | 'mfr', see
    validation.OUI_TIERS) and mirrors the high/mfr split the flock-you-esp32
    firmware uses. It defaults to 'high' so a CSV without the column — or an
    older checkout — still loads; an unrecognised value falls back to 'high'
    rather than silently downgrading a prefix.

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
            tier = (row.get("tier") or "").strip().lower()
            entries.append({
                "oui": oui,
                "vendor_context": (row.get("vendor_context") or "").strip(),
                "detection_protocol": (row.get("detection_protocol") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
                "tier": tier if is_valid_tier(tier) else OUI_TIER_DEFAULT,
                # IEEE registrant resolved from a registry snapshot — the OUI
                # belongs to this vendor, which is usually NOT Flock Safety (see
                # vendor_context for the role the prefix actually plays).
                "vendor": (row.get("vendor") or "").strip(),
            })
    return entries


def oui_tier_map(csv_path: Path = None) -> dict:
    """Return {OUI: tier} for the canonical list — used to annotate records."""
    return {entry["oui"]: entry["tier"] for entry in load_oui_metadata(csv_path)}


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
        "total_by_tier": {
            tier: sum(1 for e in entries if e.get("tier", OUI_TIER_DEFAULT) == tier)
            for tier in OUI_TIERS
        },
        "tiers": {
            "levels": list(OUI_TIERS),
            "description": (
                "'high' = assigned to Flock Safety or observed exclusively on "
                "Flock hardware; 'mfr' = contract-manufacturer prefix (Liteon/USI) "
                "that is also shipped in unrelated products."
            ),
        },
        # The classifier block is what lets the website apply exactly the same
        # confidence tiers as the collector, without duplicating the rules in JS.
        "classifier": {
            "confidence_levels": list(CONFIDENCE_LEVELS),
            "actionable_levels": list(CONFIDENCE_ACTIONABLE),
            "descriptions": {lvl: CONFIDENCE_DESCRIPTIONS[lvl] for lvl in CONFIDENCE_LEVELS},
            "ssid_confirm_patterns": list(SSID_CONFIRM_PATTERNS),
            "ssid_denylist_patterns": list(SSID_DENYLIST_PATTERNS),
            # The actual rules, in order: label + regex + reason. The browser
            # compiles these, so a record cannot be excluded in Python but counted
            # in JavaScript (or vice versa).
            "ssid_denylist_rules": [
                {"label": label, "regex": regex, "why": why}
                for label, regex, why in SSID_DENYLIST_RULES
            ],
            "ssid_denylist_notes": {
                pat: SSID_DENYLIST_NOTES[pat] for pat in SSID_DENYLIST_PATTERNS
            },
            "primary_market_countries": list(PRIMARY_MARKET_COUNTRIES),
        },
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
    by_tier = {tier: 0 for tier in OUI_TIERS}
    for entry in entries:
        by_tier[entry["tier"]] += 1
    tier_summary = ", ".join(f"{tier}={count}" for tier, count in by_tier.items())
    print(f"[✓] Wrote {len(entries)} OUI entries ({tier_summary}) → {path}")
