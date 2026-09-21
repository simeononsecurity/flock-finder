#!/usr/bin/env python3
"""
update_confidence_stats.py — render the honest headline from the published data.

Why this exists
---------------
The README used to lead with "Cameras Mapped: 146,526", a figure that counted
every OUI match equally — including the ~61,000 records whose own SSID proves
they are something else (ClickShare units, phone hotspots, gateway APs). The
caveat that explained this sat far below the fold, so the top-line number and
every map pin presented all 146,526 as equally likely cameras.

This script recomputes the figures from the *published* dataset and renders them
into the three surfaces that need to agree:

    data/scan_stats.json   machine-readable breakdown (confidence_summary, ...)
    README.md              <!-- STATS_START/END --> and the breakdown block
    docs/index.html        hero breakdown chips

It is the single writer of the README headline: scripts/wigle_query.py used to
render that block from scan_stats.json, which duplicated the counting rules and
let the headline drift from the dataset. Now the CSV is the only input.

Run it after scripts/wigle_query.py (as .github/workflows/update-data.yml does),
or standalone to refresh the published numbers from the committed CSV.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
CAMERAS_CSV = DATA_DIR / "flock_cameras.csv"
STATS_JSON = DATA_DIR / "scan_stats.json"
README = PROJECT_DIR / "README.md"
INDEX_HTML = PROJECT_DIR / "docs" / "index.html"

sys.path.insert(0, str(SCRIPT_DIR))

from oui_metadata import load_oui_metadata  # noqa: E402
from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_SSID_CONFIRMED,
    annotate_record,
    summarize_confidence,
)

RETENTION_DAYS_DEFAULT = 730

# Marker pairs owned by this script.
STATS_MARKERS = ("<!-- STATS_START -->", "<!-- STATS_END -->")
BREAKDOWN_MARKERS = ("<!-- CONFIDENCE_BREAKDOWN_START -->", "<!-- CONFIDENCE_BREAKDOWN_END -->")


# ─── Input ────────────────────────────────────────────────────────────────────

def load_published_records(csv_path: Path = CAMERAS_CSV):
    """
    Stream the published combined CSV, yielding annotated records.

    Reads only what the classifier needs, so the 24 MB CSV is never held in
    memory as a whole list.
    """
    tiers = {entry["oui"]: entry["tier"] for entry in load_oui_metadata()}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["oui_match"] = (row.get("oui_match") or "").upper()
            yield annotate_record(row, tiers)


def load_stats() -> dict:
    if not STATS_JSON.exists():
        return {}
    try:
        return json.loads(STATS_JSON.read_text())
    except Exception:
        return {}


# ─── Renderers (pure — covered by tests) ──────────────────────────────────────
#
# Each renderer returns the *inner* text of its marker block; _replace_markers()
# adds the surrounding markers. Keeping the numbers out of the marker lines means
# a rendered block can be diffed and re-rendered idempotently.

def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


def render_stats_block(summary: dict, oui_with_data: int, oui_total: int,
                       timestamp: str, retention_days: int) -> str:
    """README stats table. 'Cameras Mapped' is the actionable figure."""
    by_conf = summary["by_confidence"]
    actionable = (by_conf[CONFIDENCE_SSID_CONFIRMED] + by_conf[CONFIDENCE_OUI_HIGH]
                  + by_conf[CONFIDENCE_OUI_MFR])
    return (
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| 📸 **Cameras Mapped** (actionable) | {actionable:,} |\n"
        f"| 🔎 *of which SSID-confirmed* | {by_conf[CONFIDENCE_SSID_CONFIRMED]:,} |\n"
        f"| 🛰️ *of which OUI-suspected (high tier)* | {by_conf[CONFIDENCE_OUI_HIGH]:,} |\n"
        f"| 🧩 *of which OUI-suspected (contract-mfr tier)* | {by_conf[CONFIDENCE_OUI_MFR]:,} |\n"
        f"| 🚫 **Excluded — SSID is other hardware** | {by_conf[CONFIDENCE_IDENTIFIED_OTHER]:,} |\n"
        f"| 🌍 **Flagged outside the US** | {summary['out_of_market']:,} |\n"
        f"| 📡 **OUI Prefixes with Data** | {oui_with_data} / {oui_total} |\n"
        f"| 🌎 **Countries** | {summary['countries']} |\n"
        f"| 🗺️ **Regions / provinces (distinct)** | {summary['regions']:,} |\n"
        f"| 🕐 **Last Updated** | {timestamp} |\n"
        f"| 📦 **Data Retention** | {retention_days} days ({retention_days // 365} years) |"
    )


def render_breakdown_md(summary: dict) -> str:
    """README 'what the numbers mean' block."""
    total = summary["total"]
    by_conf = summary["by_confidence"]
    denylist = summary.get("ssid_denylist_hits", {})
    denylist_txt = " · ".join(
        f"`{pattern}*` {hits:,}"
        for pattern, hits in sorted(denylist.items(), key=lambda kv: -kv[1])
    ) or "none"

    rows = (
        ("ssid_confirmed", CONFIDENCE_SSID_CONFIRMED,
         "SSID is a Flock naming pattern (`Flock`, `Flock-XXXXXX`, "
         "`Flock Camera net.`, `FS Ext Battery`) — strongest signal passive WiFi can give"),
        ("oui_high", CONFIDENCE_OUI_HIGH,
         "High-confidence Flock OUI, SSID absent/hidden/unrecognised — suspected, unverified"),
        ("oui_mfr", CONFIDENCE_OUI_MFR,
         "Contract-manufacturer OUI (Liteon/USI) — weakest evidence, expect false positives"),
        ("identified_other", CONFIDENCE_IDENTIFIED_OTHER,
         "SSID positively identifies other hardware — excluded from the map and from the "
         "camera count above"),
    )
    lines = [
        "### What the numbers mean",
        "",
        "Every published record carries a `confidence` field, because an OUI match on its "
        "own is weak evidence. The map and the headline count only the records that survive "
        "that classification.",
        "",
        "| Confidence | Records | Share | Meaning |",
        "|------------|---------|-------|---------|",
    ]
    for label, key, meaning in rows:
        lines.append(f"| `{label}` | {by_conf[key]:,} | {_pct(by_conf[key], total)} | {meaning} |")
    lines += [
        f"| **All records** | **{total:,}** | 100.0% | |",
        "",
        f"SSID denylist hits: {denylist_txt}.",
        "",
        f"Market: {summary['out_of_market']:,} records "
        f"({_pct(summary['out_of_market'], total)}) are outside the primary US market and "
        "carry `\"out_of_market\": true`. They are flagged, not deleted — Flock has expanded "
        "internationally — so consumers can filter them.",
        "",
        "Every record is still *suspected*: the tiers describe the strength of the evidence, "
        "not a confirmation from Flock that a device is theirs. See "
        "[docs/DATA_POLICY.md](docs/DATA_POLICY.md).",
    ]
    return "\n".join(lines)


def render_hero_chips_html(summary: dict) -> str:
    """Hero breakdown chips for docs/index.html (visible before any JS runs)."""
    by_conf = summary["by_confidence"]
    return (
        '    <div class="tier-strip" id="tier-strip">\n'
        f'        <span class="tier-chip tier-ssid"><strong>{by_conf[CONFIDENCE_SSID_CONFIRMED]:,}</strong> SSID-confirmed</span>\n'
        f'        <span class="tier-chip tier-high"><strong>{by_conf[CONFIDENCE_OUI_HIGH]:,}</strong> OUI-suspected (high)</span>\n'
        f'        <span class="tier-chip tier-mfr"><strong>{by_conf[CONFIDENCE_OUI_MFR]:,}</strong> OUI-suspected (contract-mfr)</span>\n'
        f'        <span class="tier-chip tier-other"><strong>{by_conf[CONFIDENCE_IDENTIFIED_OTHER]:,}</strong> excluded — other hardware</span>\n'
        f'        <span class="tier-chip tier-market"><strong>{summary["out_of_market"]:,}</strong> flagged outside the US</span>\n'
        "    </div>\n"
        f'    <p class="tier-note" id="tier-note">“Cameras Mapped” counts the '
        f'{summary["actionable"]:,} records that pass evidence classification, out of '
        f'{summary["total"]:,} published. '
        '<a href="#confidence-breakdown">How the tiers work →</a></p>'
    )


# ─── Output ───────────────────────────────────────────────────────────────────

def _replace_markers(content: str, start: str, end: str, inner: str) -> str:
    """
    Replace the block between `start` and `end` markers with `inner`.

    Raises ValueError when the marker pair is missing, so a silently-unrendered
    headline (the very bug this script exists to prevent) fails loudly instead.
    """
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(content):
        raise ValueError(f"marker pair not found: {start} .. {end}")
    return pattern.sub(lambda _m: f"{start}\n{inner}\n{end}", content, count=1)


def _atomic_write_text(path: Path, text: str) -> None:
    """
    Write `text` to `path` atomically (tmp file + fsync + os.replace).

    Local copy of the collector's helper: validation.py must stay free of I/O
    (see CONTRIBUTING) and importing wigle_query.py would drag requests/dotenv
    into a pure renderer.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def refresh_scan_stats(summary: dict) -> Path:
    """
    Refresh the derived confidence keys in scan_stats.json.

    Only derived keys are touched — scan_timestamp, api_requests,
    cameras_by_region and the rest stay exactly as scripts/wigle_query.py wrote
    them, so an incremental scan's provenance is preserved.
    """
    stats = load_stats()
    stats["total_cameras"] = summary["total"]
    stats["total_actionable"] = summary["actionable"]
    stats["by_confidence"] = summary["by_confidence"]
    stats["by_oui_tier"] = summary["by_oui_tier"]
    stats["out_of_market"] = summary["out_of_market"]
    stats["confidence_summary"] = summary
    stats["confidence_refreshed"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(STATS_JSON, json.dumps(stats, indent=2) + "\n")
    return STATS_JSON


def main() -> int:
    if not CAMERAS_CSV.exists():
        print(f"[!] {CAMERAS_CSV} not found — nothing to summarise.")
        print("    Run scripts/wigle_query.py first, or download the dataset release "
              "(see README: 'Where is the data?').")
        return 1

    summary = summarize_confidence(load_published_records())
    stats = load_stats()
    oui_total = len(load_oui_metadata())
    oui_with_data = stats.get("unique_ouis_found", 0)
    retention = stats.get("data_retention_days", RETENTION_DAYS_DEFAULT)
    timestamp = (stats.get("scan_timestamp") or datetime.now(timezone.utc).isoformat())[:10]

    refresh_scan_stats(summary)

    readme = README.read_text(encoding="utf-8")
    readme = _replace_markers(
        readme, *STATS_MARKERS,
        render_stats_block(summary, oui_with_data, oui_total, timestamp, retention),
    )
    readme = _replace_markers(readme, *BREAKDOWN_MARKERS, render_breakdown_md(summary))
    _atomic_write_text(README, readme)

    html = INDEX_HTML.read_text(encoding="utf-8")
    html = _replace_markers(html, *BREAKDOWN_MARKERS, render_hero_chips_html(summary))
    _atomic_write_text(INDEX_HTML, html)

    by_conf = summary["by_confidence"]
    print(f"[✓] {summary['total']:,} records → {summary['actionable']:,} actionable "
          f"(ssid_confirmed={by_conf[CONFIDENCE_SSID_CONFIRMED]:,}, "
          f"oui_high={by_conf[CONFIDENCE_OUI_HIGH]:,}, "
          f"oui_mfr={by_conf[CONFIDENCE_OUI_MFR]:,}, "
          f"identified_other={by_conf[CONFIDENCE_IDENTIFIED_OTHER]:,})")
    print(f"[✓] Out-of-market: {summary['out_of_market']:,} | "
          f"OUI list: {oui_with_data}/{oui_total} | README + index.html updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

