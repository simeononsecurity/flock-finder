"""Tests for the evidence-tier headline renderer (scripts/update_confidence_stats.py).

That script is the single writer of the README stats block and the website hero
chips, so these tests pin the formatting *and* — most importantly — that the
published headline still adds up to the dataset behind it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from update_confidence_stats import (  # noqa: E402
    BREAKDOWN_MARKERS,
    README,
    STATS_MARKERS,
    _replace_markers,
    load_candidate_total,
    render_breakdown_md,
    render_hero_chips_html,
    render_stats_block,
    summary_from_stats,
)
from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_SSID_CORRELATED,
)

SUMMARY = {
    "total": 1000,
    "actionable": 850,
    "by_confidence": {
        CONFIDENCE_SSID_CORRELATED: 50,
        CONFIDENCE_OUI_HIGH: 700,
        CONFIDENCE_OUI_MFR: 100,
        CONFIDENCE_IDENTIFIED_OTHER: 150,
    },
    "by_oui_tier": {"high": 750, "mfr": 100},
    # Per-prefix measurement. (These three are a subset of the fixture's 1,000
    # records — the rest belong to prefixes below the 200-record display floor.)
    "by_oui_confidence": {
        "70:C9:4E": {CONFIDENCE_SSID_CORRELATED: 240, CONFIDENCE_OUI_HIGH: 160},
        "F4:6A:DD": {CONFIDENCE_OUI_MFR: 20, CONFIDENCE_IDENTIFIED_OTHER: 480},
        "A4:CF:12": {CONFIDENCE_OUI_HIGH: 300},
    },
    "out_of_market": 400,
    "actionable_out_of_market": 300,
    "ssid_denylist_hits": {"clickshare": 120, "detector_verdict": 30},
    "ssid_tool_verdicts": {"low": 26, "medium": 4},
    "countries": 12,
    "regions": 40,
    "candidate_total": 7,
    "unrelated_excluded": 150,
}


# ─── Renderers ────────────────────────────────────────────────────────────────

def test_stats_block_leads_with_correlation_and_combines_oui_only():
    block = render_stats_block(SUMMARY, 31, 38, "2026-01-02", 730, 7)
    assert "| 🔎 **SSID-correlated sightings** | 50 |" in block
    assert "| 📡 **OUI-only candidate sightings** (suspected, unverified) | 800 |" in block
    assert "| 🗺️ **Total public candidate sightings** | 850 |" in block
    assert "| 📊 **Observed/source total** | 1,000 |" in block
    assert "| 🧭 **Novel OUI-prefix candidates** | 7 |" in block
    assert "| 🚫 **Unrelated-device exclusions** | 150 |" in block
    assert "| 📡 **OUI Prefixes with Data** | 31 / 38 |" in block
    assert "| 🕐 **Last Updated** | 2026-01-02 |" in block
    assert "Cameras Mapped" not in block
    assert "actionable" not in block.lower()
    assert "OUI-suspected (high tier)" not in block


def test_breakdown_combines_oui_tiers_and_keeps_exclusions_aggregate_only():
    block = render_breakdown_md(SUMMARY)
    assert "`ssid_correlated`" in block
    assert "`oui_only`" in block
    assert "`oui_high`" not in block
    assert "`oui_mfr`" not in block
    assert "| **All published observations** | **850** | 100.0% | |" in block
    assert "Unrelated-device exclusions: 150" in block
    assert "`clickshare*` 120" in block      # denylist evidence is quoted
    assert "`detector_verdict*` 30" in block
    assert "47.1%" in block                  # public out-of-market share
    assert "suspected" in block              # policy caveat survives rendering
    assert "ssid_confirmed" not in block


def test_breakdown_calls_out_detector_output_with_its_verdict_tags():
    block = render_breakdown_md(SUMMARY)
    assert "Detector output ingested as data" in block
    assert "another detector's verdict string" in block
    assert "`low` 26" in block
    assert "`medium` 4" in block


def test_breakdown_measures_prefixes_three_ways():
    block = render_breakdown_md(SUMMARY)
    # F4:6A:DD is 96% other-hardware in the fixture → noisy table.
    assert "| `F4:6A:DD` | 500 | 0 | 480 (96%) |" in block
    # 70:C9:4E is 60% SSID-correlated, which forms the contrast line.
    assert "`70:C9:4E` (60% SSID-correlated, 240 of 400 records)" in block
    # A4:CF:12 has neither correlation nor exclusion, so it stays unverified. The
    # old single-share metric wrongly called it "clean".
    assert "neither correlated nor excluded" in block
    assert "`A4:CF:12` (300 records, 0 SSID-correlated)" in block
    assert "clean" not in block


def test_hero_chips_expose_all_five_numbers():
    html = render_hero_chips_html(SUMMARY)
    for number in ("50", "800", "850", "150", "400"):
        assert f"<strong>{number}</strong>" in html
    assert 'id="tier-strip"' in html
    assert "OUI-only candidate sightings" in html
    assert "suspected matches, not confirmed devices" in html
    assert "Cameras Mapped" not in html


def test_marker_replacement_is_idempotent():
    doc = f"before\n{STATS_MARKERS[0]}\nstale\n{STATS_MARKERS[1]}\nafter\n"
    once = _replace_markers(doc, *STATS_MARKERS,
                            render_stats_block(SUMMARY, 1, 2, "d", 1))
    twice = _replace_markers(once, *STATS_MARKERS,
                             render_stats_block(SUMMARY, 1, 2, "d", 1))
    assert once == twice
    assert "stale" not in once
    assert "before" in once and "after" in once


def test_missing_markers_raise_instead_of_silently_skipping():
    with pytest.raises(ValueError):
        _replace_markers("no markers here", *STATS_MARKERS, "x")


def test_refresh_scan_stats_is_idempotent(tmp_path, monkeypatch):
    """
    Re-running the renderer on unchanged data must not rewrite scan_stats.json:
    the refresh timestamp alone would dirty the tree — and trip the CI bot's
    `git diff` change check — on every run.
    """
    import json

    from update_confidence_stats import refresh_scan_stats

    stats_path = tmp_path / "scan_stats.json"
    stats_path.write_text(json.dumps({"scan_timestamp": "2026-01-01T00:00:00Z"}) + "\n")
    monkeypatch.setattr("update_confidence_stats.STATS_JSON", stats_path)

    assert refresh_scan_stats(SUMMARY) is not None
    first = stats_path.read_text()
    assert json.loads(first)["total_actionable"] == 850

    # Second call with identical input: no rewrite, no new timestamp.
    assert refresh_scan_stats(SUMMARY) is None
    assert stats_path.read_text() == first

    # A changed number does rewrite.
    changed = dict(SUMMARY, actionable=851)
    changed["by_confidence"] = dict(SUMMARY["by_confidence"])
    assert refresh_scan_stats(changed) is not None
    assert json.loads(stats_path.read_text())["total_actionable"] == 851


def test_legacy_scan_stats_normalize_without_legacy_public_wording():
    summary = summary_from_stats({
        "by_confidence": {
            "ssid_confirmed": 5,
            "oui_high": 70,
            "oui_mfr": 10,
            "identified_other": 15,
        },
        "confidence_summary": {
            "actionable_out_of_market": 12,
            "countries": 3,
            "regions": 4,
            "by_oui_confidence": {
                "70:C9:4E": {"ssid_confirmed": 5, "oui_high": 7},
            },
        },
    })
    assert summary["by_confidence"][CONFIDENCE_SSID_CORRELATED] == 5
    assert summary["total"] == 85
    assert summary["unrelated_excluded"] == 15
    assert summary["out_of_market"] == 12
    rendered = render_stats_block(summary, 1, 1, "2026-01-01", 730)
    assert "ssid_confirmed" not in rendered
    assert "OUI-only candidate sightings" in rendered


def test_suppression_aware_stats_use_registry_aggregate_not_public_rows():
    summary = summary_from_stats({
        "suppression_applied": True,
        "by_confidence": {
            "ssid_correlated": 5,
            "oui_high": 70,
            "oui_mfr": 10,
            "identified_other": 999,
        },
        "suppression": {
            "registry_entries": 42,
            "excluded_from_release": 8,
            "registry_by_reason": {"clickshare": 42},
        },
        "out_of_market": 12,
    })
    assert summary["unrelated_excluded"] == 42
    assert summary["total"] == 85
    assert summary["by_confidence"][CONFIDENCE_IDENTIFIED_OTHER] == 0
    assert summary["observed_total"] == 127


def test_suppression_stats_shape_uses_aggregate_count_without_csv_rows():
    summary = summary_from_stats({
        "by_confidence": {"ssid_correlated": 5, "oui_high": 70, "oui_mfr": 10},
        "suppression_stats": {"excluded_count": 42, "by_reason": {"clickshare": 42}},
        "out_of_market": 12,
    })
    assert summary["unrelated_excluded"] == 42
    assert summary["observed_total"] == 127


def test_current_scan_stats_tracked_values_and_html_text():
    import json

    stats_path = os.path.join(os.path.dirname(__file__), "..", "data", "scan_stats.json")
    stats = json.loads(open(stats_path, encoding="utf-8").read())
    summary = summary_from_stats(stats)
    assert summary["by_confidence"][CONFIDENCE_SSID_CORRELATED] == 5100
    assert summary["by_confidence"][CONFIDENCE_OUI_HIGH] + summary["by_confidence"][CONFIDENCE_OUI_MFR] == 85999
    assert summary["unrelated_excluded"] == 62961
    assert summary["observed_total"] == 154060
    summary["candidate_total"] = 0
    html = render_hero_chips_html(summary)
    assert "5,100</strong> SSID-correlated sightings" in html
    assert "85,999</strong> OUI-only candidate sightings" in html
    assert "91,099</strong> total public candidate sightings" in html
    assert "62,961</strong> unrelated-device exclusions" in html


def test_candidate_total_reads_tracked_candidate_metadata(tmp_path):
    path = tmp_path / "candidate_ouis.json"
    path.write_text('{"total_candidates": 9}', encoding="utf-8")
    assert load_candidate_total(path) == 9


# ─── Drift guard: the published headline must describe the published dataset ──

def test_headline_renderer_matches_tracked_scan_stats():
    """
    If this fails, the committed README no longer describes the committed
    dataset — re-run `python3 scripts/update_confidence_stats.py`.
    """
    import json

    stats_path = os.path.join(os.path.dirname(__file__), "..", "data", "scan_stats.json")
    stats = json.loads(open(stats_path, encoding="utf-8").read())
    summary = summary_from_stats(stats)
    block = render_stats_block(summary, 0, 0, "2026-01-01", 730)
    oui_only = (
        summary["by_confidence"][CONFIDENCE_OUI_HIGH]
        + summary["by_confidence"][CONFIDENCE_OUI_MFR]
    )
    assert f"{summary['by_confidence'][CONFIDENCE_SSID_CORRELATED]:,}" in block
    assert f"{oui_only:,}" in block


def test_readme_and_index_declare_both_marker_pairs():
    """A missing marker pair would silently stop a generated block updating."""
    readme = README.read_text(encoding="utf-8")
    for marker in STATS_MARKERS + BREAKDOWN_MARKERS:
        assert marker in readme, marker

    index = (os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"))
    html = open(index, encoding="utf-8").read()
    for marker in BREAKDOWN_MARKERS:
        assert marker in html, marker
