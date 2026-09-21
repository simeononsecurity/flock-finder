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
    CAMERAS_CSV,
    README,
    STATS_MARKERS,
    _replace_markers,
    load_published_records,
    render_breakdown_md,
    render_hero_chips_html,
    render_stats_block,
)
from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_SSID_CONFIRMED,
    summarize_confidence,
)

SUMMARY = {
    "total": 1000,
    "actionable": 850,
    "by_confidence": {
        CONFIDENCE_SSID_CONFIRMED: 50,
        CONFIDENCE_OUI_HIGH: 700,
        CONFIDENCE_OUI_MFR: 100,
        CONFIDENCE_IDENTIFIED_OTHER: 150,
    },
    "by_oui_tier": {"high": 750, "mfr": 100},
    "out_of_market": 400,
    "actionable_out_of_market": 300,
    "ssid_denylist_hits": {"clickshare": 120, "flock alpr [": 30},
    "countries": 12,
    "regions": 40,
}


# ─── Renderers ────────────────────────────────────────────────────────────────

def test_stats_block_headline_is_the_actionable_count():
    block = render_stats_block(SUMMARY, 31, 38, "2026-01-02", 730)
    assert "| 📸 **Cameras Mapped** (actionable) | 850 |" in block
    assert "| 🔎 *of which SSID-confirmed* | 50 |" in block
    assert "| 🚫 **Excluded — SSID is other hardware** | 150 |" in block
    assert "| 📡 **OUI Prefixes with Data** | 31 / 38 |" in block
    assert "| 🕐 **Last Updated** | 2026-01-02 |" in block


def test_breakdown_lists_every_tier_with_shares():
    block = render_breakdown_md(SUMMARY)
    for label in ("ssid_confirmed", "oui_high", "oui_mfr", "identified_other"):
        assert f"`{label}`" in block
    assert "| **All records** | **1,000** | 100.0% | |" in block
    assert "`clickshare*` 120" in block      # denylist evidence is quoted
    assert "`flock alpr [*` 30" in block     # self-referential tool text named
    assert "40.0%" in block                  # out-of-market share
    assert "suspected" in block              # policy caveat survives rendering


def test_hero_chips_expose_all_five_numbers():
    html = render_hero_chips_html(SUMMARY)
    for number in ("50", "700", "100", "150", "400"):
        assert f"<strong>{number}</strong>" in html
    assert 'id="tier-strip"' in html
    assert "850 records that" in html        # actionable total in the note


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


# ─── Drift guard: the published headline must describe the published dataset ──

def test_readme_headline_matches_the_dataset():
    """
    If this fails, the committed README no longer describes the committed
    dataset — re-run `python3 scripts/update_confidence_stats.py`.
    """
    if not CAMERAS_CSV.exists():
        pytest.skip("published dataset absent (not committed — see README)")

    summary = summarize_confidence(load_published_records())
    readme = README.read_text(encoding="utf-8")
    block = readme.split(STATS_MARKERS[0], 1)[1].split(STATS_MARKERS[1], 1)[0]

    assert f"| {summary['actionable']:,} |" in block
    for key in (CONFIDENCE_SSID_CONFIRMED, CONFIDENCE_OUI_HIGH,
                CONFIDENCE_OUI_MFR, CONFIDENCE_IDENTIFIED_OTHER):
        assert f"{summary['by_confidence'][key]:,}" in block, key


def test_readme_and_index_declare_both_marker_pairs():
    """A missing marker pair would silently stop a generated block updating."""
    readme = README.read_text(encoding="utf-8")
    for marker in STATS_MARKERS + BREAKDOWN_MARKERS:
        assert marker in readme, marker

    index = (os.path.join(os.path.dirname(__file__), "..", "docs", "index.html"))
    html = open(index, encoding="utf-8").read()
    for marker in BREAKDOWN_MARKERS:
        assert marker in html, marker
