#!/usr/bin/env python3
"""
update_confidence_stats.py — render the honest headline from the published data.

Why this exists
---------------
The public headline separates anchored SSID correlations, OUI-only
observations, candidate prefixes, and aggregate unrelated-device exclusions.

This script normalizes tracked scan statistics and renders them
into the three surfaces that need to agree:

    data/scan_stats.json   machine-readable breakdown (confidence_summary, ...)
    README.md              <!-- STATS_START/END --> and the breakdown block
    docs/index.html        hero breakdown chips

It is the single writer of the README headline. Current suppression-aware
statistics and legacy confidence keys share one compatibility path.

Run it after scripts/wigle_query.py, or standalone to refresh the tracked
surfaces from data/scan_stats.json and data/candidate_ouis.json.
"""

from __future__ import annotations

import argparse
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
CANDIDATE_OUIS_JSON = DATA_DIR / "candidate_ouis.json"
SUPPRESSION_REGISTRY_JSON = DATA_DIR / "suppression_registry.json"
README = PROJECT_DIR / "README.md"
INDEX_HTML = PROJECT_DIR / "docs" / "index.html"
CLASSIFIER_REPORT = DATA_DIR / "validation" / "ssid_metrics.json"

sys.path.insert(0, str(SCRIPT_DIR))

from oui_metadata import load_oui_metadata  # noqa: E402
from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_SSID_CORRELATED,
    LEGACY_CONFIDENCE_SSID_CONFIRMED,
    annotate_record,
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


def load_candidate_total(path: Path = CANDIDATE_OUIS_JSON) -> int:
    """Read the separately published candidate OUI-prefix total."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    total = payload.get("total_candidates") if isinstance(payload, dict) else None
    return total if isinstance(total, int) and total >= 0 else 0


def summary_from_stats(stats: dict) -> dict:
    """Normalize current and legacy scan statistics for public rendering."""
    if not isinstance(stats, dict):
        raise ValueError("scan statistics must be an object")
    base = stats.get("confidence_summary")
    if not isinstance(base, dict):
        base = {}
    raw_by_conf = stats.get("by_confidence")
    if not isinstance(raw_by_conf, dict):
        raw_by_conf = base.get("by_confidence") or {}

    correlated = raw_by_conf.get(CONFIDENCE_SSID_CORRELATED)
    if not isinstance(correlated, int):
        correlated = raw_by_conf.get(LEGACY_CONFIDENCE_SSID_CONFIRMED, 0)
    oui_high = raw_by_conf.get(CONFIDENCE_OUI_HIGH, 0)
    oui_mfr = raw_by_conf.get(CONFIDENCE_OUI_MFR, 0)
    correlated = correlated if isinstance(correlated, int) else 0
    oui_high = oui_high if isinstance(oui_high, int) else 0
    oui_mfr = oui_mfr if isinstance(oui_mfr, int) else 0
    public_total = correlated + oui_high + oui_mfr

    suppression = stats.get("suppression")
    suppression = suppression if isinstance(suppression, dict) else {}
    suppression_stats = stats.get("suppression_stats")
    suppression_stats = suppression_stats if isinstance(suppression_stats, dict) else {}
    registry = stats.get("suppression_registry")
    registry = registry if isinstance(registry, dict) else {}
    registry_count = None
    for source in (suppression, suppression_stats, stats, registry):
        for key in ("registry_entries", "excluded_from_release", "excluded_count", "total_excluded"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                registry_count = value
                break
        if registry_count is not None:
            break
    suppression_aware = bool(stats.get("suppression_applied") or suppression or suppression_stats or registry)
    if suppression_aware:
        excluded = registry_count if registry_count is not None else 0
        denylist_hits = (
            suppression.get("registry_by_reason") or suppression.get("by_reason")
            or suppression_stats.get("by_reason") or registry.get("by_reason") or {}
        )
        public_out_of_market = stats.get("out_of_market", base.get("out_of_market", 0))
    else:
        excluded = raw_by_conf.get(CONFIDENCE_IDENTIFIED_OTHER, 0)
        denylist_hits = base.get("ssid_denylist_hits", {})
        public_out_of_market = base.get(
            "actionable_out_of_market",
            stats.get("out_of_market", base.get("out_of_market", 0)),
        )
    excluded = excluded if isinstance(excluded, int) and not isinstance(excluded, bool) else 0
    if suppression_aware and registry_count is None and SUPPRESSION_REGISTRY_JSON.exists():
        try:
            payload = json.loads(SUPPRESSION_REGISTRY_JSON.read_text(encoding="utf-8"))
            count = payload.get("entry_count") if isinstance(payload, dict) else None
            if not isinstance(count, int) and isinstance(payload, dict):
                entries = payload.get("entries")
                count = len(entries) if isinstance(entries, (dict, list)) else None
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                excluded = count
        except (OSError, ValueError):
            pass
    public_out_of_market = public_out_of_market if isinstance(public_out_of_market, int) else 0

    per_prefix = stats.get("by_oui_confidence")
    if not isinstance(per_prefix, dict):
        per_prefix = base.get("by_oui_confidence") or {}
    normalized_prefixes = {}
    for oui, counts in per_prefix.items():
        if not isinstance(counts, dict):
            continue
        normalized = dict(counts)
        if CONFIDENCE_SSID_CORRELATED not in normalized:
            normalized[CONFIDENCE_SSID_CORRELATED] = normalized.pop(
                LEGACY_CONFIDENCE_SSID_CONFIRMED, 0
            )
        normalized_prefixes[oui] = normalized

    observed_total = stats.get("observed_total", stats.get("source_total"))
    if not isinstance(observed_total, int) or isinstance(observed_total, bool):
        observed_total = public_total + excluded
    return {
        "total": public_total,
        "actionable": public_total,
        "observed_total": observed_total,
        "unrelated_excluded": excluded,
        "by_confidence": {
            CONFIDENCE_SSID_CORRELATED: correlated,
            CONFIDENCE_OUI_HIGH: oui_high,
            CONFIDENCE_OUI_MFR: oui_mfr,
            CONFIDENCE_IDENTIFIED_OTHER: 0,
        },
        "by_oui_tier": stats.get("by_oui_tier", base.get("by_oui_tier", {})),
        "by_oui_confidence": normalized_prefixes,
        "out_of_market": public_out_of_market,
        "actionable_out_of_market": public_out_of_market,
        "ssid_denylist_hits": denylist_hits if isinstance(denylist_hits, dict) else {},
        "ssid_tool_verdicts": base.get("ssid_tool_verdicts", stats.get("ssid_tool_verdicts", {})),
        "countries": base.get("countries", len(stats.get("cameras_by_country") or {})),
        "regions": base.get("regions", len(stats.get("cameras_by_region") or {})),
    }


# ─── Renderers (pure — covered by tests) ──────────────────────────────────────
#
# Each renderer returns the *inner* text of its marker block; _replace_markers()
# adds the surrounding markers. Keeping the numbers out of the marker lines means
# a rendered block can be diffed and re-rendered idempotently.

def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


def render_stats_block(summary: dict, oui_with_data: int, oui_total: int,
                       timestamp: str, retention_days: int,
                       candidate_total: int = 0) -> str:
    """README stats table led by the strongest published evidence."""
    by_conf = summary["by_confidence"]
    correlated = by_conf[CONFIDENCE_SSID_CORRELATED]
    oui_only = by_conf[CONFIDENCE_OUI_HIGH] + by_conf[CONFIDENCE_OUI_MFR]
    public_total = correlated + oui_only
    excluded = summary.get("unrelated_excluded", by_conf.get(CONFIDENCE_IDENTIFIED_OTHER, 0))
    return (
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| 🔎 **SSID-correlated sightings** | {correlated:,} |\n"
        f"| 📡 **OUI-only candidate sightings** (suspected, unverified) | {oui_only:,} |\n"
        f"| 🗺️ **Total public candidate sightings** | {public_total:,} |\n"
        f"| 📊 **Observed/source total** | {summary.get('observed_total', public_total + excluded):,} |\n"
        f"| 🧭 **Novel OUI-prefix candidates** | {candidate_total:,} |\n"
        f"| 🚫 **Unrelated-device exclusions** | {excluded:,} |\n"
        f"| 🌍 **Published observations outside the US** | {summary['out_of_market']:,} |\n"
        f"| 📡 **OUI Prefixes with Data** | {oui_with_data} / {oui_total} |\n"
        f"| 🌎 **Countries** | {summary['countries']} |\n"
        f"| 🗺️ **Regions / provinces (distinct)** | {summary['regions']:,} |\n"
        f"| 🕐 **Last Updated** | {timestamp} |\n"
        f"| 📦 **Data Retention** | {retention_days} days ({retention_days // 365} years) |"
    )


def oui_signal_rows(summary: dict, min_records: int = 200) -> list:
    """
    Measured signal per OUI prefix, noisiest first.

    Returns (oui, total, correlated, excluded, correlated_share, excluded_share)
    for every prefix with at least `min_records`.

    Two shares rather than one label, because either number alone misleads: a
    prefix whose SSIDs are simply unreadable has ~0% "other hardware" *and* ~0%
    Flock-correlated, which is neither clean nor noisy — it is unverified. The OUI
    list mixes all three populations, so the measurement is published instead of
    asserting that every prefix is an equally credible fingerprint.
    """
    rows = []
    for oui, counts in (summary.get("by_oui_confidence") or {}).items():
        total = sum(counts.values())
        if total < min_records:
            continue
        correlated = counts.get(CONFIDENCE_SSID_CORRELATED, 0)
        excluded = counts.get(CONFIDENCE_IDENTIFIED_OTHER, 0)
        rows.append((oui, total, correlated, excluded, correlated / total, excluded / total))
    rows.sort(key=lambda row: (-row[5], -row[1]))
    return rows


def render_breakdown_md(summary: dict) -> str:
    """README 'what the numbers mean' block."""
    by_conf = summary["by_confidence"]
    correlated = by_conf[CONFIDENCE_SSID_CORRELATED]
    oui_only = by_conf[CONFIDENCE_OUI_HIGH] + by_conf[CONFIDENCE_OUI_MFR]
    total = correlated + oui_only
    excluded = summary.get("unrelated_excluded", by_conf.get(CONFIDENCE_IDENTIFIED_OTHER, 0))
    denylist = summary.get("ssid_denylist_hits", {})
    denylist_txt = " · ".join(
        f"`{pattern}*` {hits:,}"
        for pattern, hits in sorted(denylist.items(), key=lambda kv: -kv[1])
    ) or "none"

    lines = [
        "### What the numbers mean",
        "",
        "SSID-correlated sightings match an anchored Flock naming pattern. OUI-only "
        "observations match a configured MAC prefix but lack SSID correlation. They remain "
        "suspected and unverified.",
        "",
        "| Evidence | Observations | Share of published observations | Meaning |",
        "|----------|--------------|---------------------------------|---------|",
        f"| `ssid_correlated` | {correlated:,} | {_pct(correlated, total)} | "
        "Anchored SSID correlation, the strongest passive WiFi signal |",
        f"| `oui_only` | {oui_only:,} | {_pct(oui_only, total)} | "
        "OUI match without SSID correlation, suspected and unverified |",
        f"| **All published observations** | **{total:,}** | 100.0% | |",
        f"| **Observed/source total** | **{summary.get('observed_total', total + excluded):,}** | | Includes public candidates and aggregate unrelated-device exclusions |",
        "",
        f"Unrelated-device exclusions: {excluded:,}. These exclusions are aggregate counts, "
        "not public point records.",
    ]
    if denylist_txt != "none":
        lines += ["", f"Exclusion reasons: {denylist_txt}."]

    # Detector output ingested as data — called out separately because it is a
    # provenance problem, not a false-positive class: these records are a copy of
    # another tool's verdict, and the tool itself tagged most of them low.
    verdicts = summary.get("ssid_tool_verdicts") or {}
    if verdicts or denylist.get("detector_verdict"):
        verdict_txt = " · ".join(
            f"`{tag}` {count:,}" for tag, count in sorted(verdicts.items(), key=lambda kv: -kv[1])
        ) or "untagged"
        lines += [
            "",
            f"**Detector output ingested as data.** {denylist.get('detector_verdict', 0):,} records carry "
            "another detector's verdict string in the SSID column (`Flock ALPR [wifi_receiver_oui;low]` "
            f"and similar) — self-tagged {verdict_txt} by the tool that produced them. They are a "
            "downstream copy of a detection, not an observation of a camera, so they are excluded "
            "outright instead of being counted in the SSID-correlated subset.",
        ]

    rows = oui_signal_rows(summary)
    if rows:
        noisy = [row for row in rows if row[5] >= 0.5][:8]
        most_correlated = sorted(rows, key=lambda row: -row[4])[:3]
        unverified = [row for row in rows if row[5] < 0.5 and row[4] < 0.02]

        lines += [
            "",
            f"**Per-prefix signal, measured.** Of the {len(rows)} prefixes with ≥200 records, "
            "the ones whose records mostly name *other* hardware:",
            "",
            "| OUI prefix | Records | SSID-correlated | Excluded as other hardware |",
            "|------------|---------|----------------|----------------------------|",
        ]
        for oui, prefix_total, correlated, excluded, _cshare, share in noisy:
            lines.append(
                f"| `{oui}` | {prefix_total:,} | {correlated:,} | {excluded:,} ({share * 100:.0f}%) |"
            )
        lines += [
            "",
            "The most Flock-correlated, for contrast: "
            + ", ".join(
                f"`{oui}` ({correlated_share * 100:.0f}% SSID-correlated, "
                f"{correlated:,} of {total:,} records)"
                for oui, total, correlated, _e, correlated_share, _s in most_correlated
            ) + ".",
        ]
        if unverified:
            lines += [
                "",
                f"**{len(unverified)} prefixes are neither correlated nor excluded** — under 2% of their "
                "records carry a Flock SSID and under half name other hardware, so their MACs are simply "
                "unverifiable from SSID evidence: "
                + ", ".join(
                    f"`{oui}` ({total:,} records, {correlated:,} SSID-correlated)"
                    for oui, total, correlated, _e, _cshare, _s in unverified[:6]
                ) + (f" and {len(unverified) - 6} more" if len(unverified) > 6 else "") + "."
                " These are the weakest entries in the list: an OUI match there means little on its own.",
            ]
        lines += [
            "",
            "The `tier` field in `data/flock_ouis.csv` is the curated (firmware-parity) judgement; these "
            "numbers are the measurement. Where they disagree, the measurement is the honest summary of "
            "what this dataset actually contains.",
        ]

    lines += [
        "",
        f"Market: {summary['out_of_market']:,} published observations "
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
    correlated = by_conf[CONFIDENCE_SSID_CORRELATED]
    oui_only = by_conf[CONFIDENCE_OUI_HIGH] + by_conf[CONFIDENCE_OUI_MFR]
    public_total = correlated + oui_only
    excluded = summary.get("unrelated_excluded", by_conf.get(CONFIDENCE_IDENTIFIED_OTHER, 0))
    return (
        '    <div class="tier-strip" id="tier-strip">\n'
        f'        <span class="tier-chip tier-ssid"><strong>{correlated:,}</strong> SSID-correlated sightings</span>\n'
        f'        <span class="tier-chip tier-high"><strong>{oui_only:,}</strong> OUI-only candidate sightings</span>\n'
        f'        <span class="tier-chip tier-total"><strong>{public_total:,}</strong> total public candidate sightings</span>\n'
        f'        <span class="tier-chip tier-other"><strong>{excluded:,}</strong> unrelated-device exclusions</span>\n'
        f'        <span class="tier-chip tier-market"><strong>{summary["out_of_market"]:,}</strong> published observations outside the US</span>\n'
        "    </div>\n"
        '    <p class="tier-note" id="tier-note">OUI-only candidate sightings are suspected matches, not confirmed devices. '
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

    Returns the path when the file was rewritten, or None when every derived value
    already matched. The early return matters: without it the refresh timestamp
    alone would dirty the working tree (and trip the CI bot's `git diff` change
    check) on every run, even when nothing had changed.
    """
    stats = load_stats()
    derived = {
        "total_cameras": summary["total"],
        "total_actionable": summary["actionable"],
        "ssid_correlated": summary["by_confidence"].get(CONFIDENCE_SSID_CORRELATED, 0),
        "oui_only": (
            summary["by_confidence"].get(CONFIDENCE_OUI_HIGH, 0)
            + summary["by_confidence"].get(CONFIDENCE_OUI_MFR, 0)
        ),
        "by_confidence": summary["by_confidence"],
        "by_oui_tier": summary["by_oui_tier"],
        "out_of_market": summary["out_of_market"],
        "ssid_tool_verdicts": summary.get("ssid_tool_verdicts", {}),
        "by_oui_confidence": summary.get("by_oui_confidence", {}),
        "confidence_summary": summary,
        "classifier_validation": json.loads(CLASSIFIER_REPORT.read_text(encoding="utf-8")),
    }
    if all(stats.get(key) == value for key, value in derived.items()):
        return None

    stats.update(derived)
    stats["confidence_refreshed"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(STATS_JSON, json.dumps(stats, indent=2) + "\n")
    return STATS_JSON


def main() -> int:
    parser = argparse.ArgumentParser(description="Render tracked evidence statistics")
    parser.add_argument(
        "--readme-only",
        action="store_true",
        help="Update README marker blocks without writing docs/index.html",
    )
    args = parser.parse_args()
    stats = load_stats()
    if not stats:
        print(f"[!] {STATS_JSON} is missing or invalid. Nothing to render.")
        return 1

    summary = summary_from_stats(stats)
    # Registry aggregates are tracked separately from published point records.
    # Never consult an ignored/local scan CSV to derive public headline values.
    candidate_total = load_candidate_total()
    summary["candidate_total"] = candidate_total
    oui_total = len(load_oui_metadata())
    oui_with_data = stats.get("unique_ouis_found", 0)
    retention = stats.get("data_retention_days", RETENTION_DAYS_DEFAULT)
    timestamp = (stats.get("scan_timestamp") or datetime.now(timezone.utc).isoformat())[:10]

    readme = README.read_text(encoding="utf-8")
    readme = _replace_markers(
        readme, *STATS_MARKERS,
        render_stats_block(
            summary, oui_with_data, oui_total, timestamp, retention, candidate_total
        ),
    )
    readme = _replace_markers(readme, *BREAKDOWN_MARKERS, render_breakdown_md(summary))
    _atomic_write_text(README, readme)

    if not args.readme_only:
        html = INDEX_HTML.read_text(encoding="utf-8")
        html = _replace_markers(html, *BREAKDOWN_MARKERS, render_hero_chips_html(summary))
        _atomic_write_text(INDEX_HTML, html)

    by_conf = summary["by_confidence"]
    oui_only = by_conf[CONFIDENCE_OUI_HIGH] + by_conf[CONFIDENCE_OUI_MFR]
    print(f"[✓] SSID-correlated sightings: {by_conf[CONFIDENCE_SSID_CORRELATED]:,} | "
          f"OUI-only candidate sightings: {oui_only:,} | novel OUI candidates: {candidate_total:,} | "
          f"unrelated exclusions: {summary['unrelated_excluded']:,}")
    targets = "README" if args.readme_only else "README + index.html"
    print(f"[✓] Out-of-market: {summary['out_of_market']:,} | "
          f"OUI list: {oui_with_data}/{oui_total} | {targets} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

