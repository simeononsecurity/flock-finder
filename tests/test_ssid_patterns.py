"""Tests for the SSID discovery patterns used by scripts/ssid_query.py.

The pattern list decides which WiGLE records are scanned for *novel* OUI
prefixes, so a missing pattern is a silent blind spot (that is exactly how
`Flock Camera net.` and the `FS Ext Battery` pack series stayed invisible).
These tests pin the coverage.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from ssid_query import FLOCK_SSID_PATTERNS  # noqa: E402


def test_patterns_cover_every_known_name_prefix():
    patterns = [pattern for pattern, _ in FLOCK_SSID_PATTERNS]
    assert "Flock%" in patterns
    assert "FLOCK%" in patterns
    # FS Ext Battery accessory series — invisible to Flock-* matching.
    assert "FS Ext Battery%" in patterns
    assert "fs ext battery%" in patterns


def test_patterns_are_unique_wildcards_with_descriptions():
    patterns = [pattern for pattern, _ in FLOCK_SSID_PATTERNS]
    assert len(patterns) == len(set(patterns)), "duplicate SSID pattern"
    for pattern, description in FLOCK_SSID_PATTERNS:
        assert pattern.endswith("%"), f"{pattern} is not a wildcard pattern"
        assert description.strip(), f"{pattern} has no description"


def test_battery_pattern_stays_specific_to_fs_ext_battery():
    """
    The pattern must remain qualified by "FS Ext Battery" — a bare `battery%`
    wildcard would sweep in unrelated consumer hardware and flood the candidate
    OUI list with false leads.
    """
    for pattern, _ in FLOCK_SSID_PATTERNS:
        assert not pattern.lower().startswith("battery"), pattern


# ─── Camera-class coverage (the LAA class used to fail silently) ──────────────

def test_count_pattern_hits_is_case_sensitive_like_wigle():
    from ssid_query import count_pattern_hits

    networks = [
        {"ssid": "Flock-7EBB9D"},
        {"ssid": "Flock Camera net."},
        {"ssid": "FLOCK-123456"},
        {"ssid": "FS Ext Battery"},
        {"ssid": "FS Ext Battery 1234"},
        {"ssid": "ClickShare"},
    ]
    hits = count_pattern_hits(networks, FLOCK_SSID_PATTERNS)
    assert hits["Flock%"] == 2
    assert hits["FLOCK%"] == 1
    assert hits["FS Ext Battery%"] == 2
    assert hits["fs ext battery%"] == 0


def test_summarize_dataset_coverage_counts_every_class(tmp_path):
    import csv as _csv

    from ssid_query import summarize_dataset_coverage

    path = tmp_path / "flock_cameras.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=["netid", "ssid", "country"])
        writer.writeheader()
        writer.writerow({"netid": "82:6B:F2:00:00:01", "ssid": "Flock", "country": "US"})
        writer.writerow({"netid": "70:C9:4E:00:00:02", "ssid": "Flock Camera net.", "country": "US"})
        writer.writerow({"netid": "70:C9:4E:00:00:03", "ssid": "FS Ext Battery", "country": "US"})
        writer.writerow({"netid": "70:C9:4E:00:00:04", "ssid": "", "country": "US"})

    coverage = summarize_dataset_coverage(path)
    assert coverage["records_scanned"] == 4
    assert coverage["ssid_bearing"] == 3
    assert coverage["flock_camera_net_records"] == 1
    assert coverage["fs_ext_battery_records"] == 1
    assert coverage["laa_records"] == 1          # 0x82 has the LAA bit set
    assert coverage["laa_by_oui"] == {"82:6B:F2": 1}


def test_zero_laa_class_is_stated_explicitly_not_left_empty():
    """
    The issue-#43 class had no records and said nothing, which read as an
    unsearched class. A zero must be published with its interpretation.
    """
    from ssid_query import coverage_note

    note = coverage_note({
        "flock_camera_net_records": 0,
        "fs_ext_battery_records": 0,
        "laa_records": 0,
        "laa_by_oui": {},
    })
    assert "zero records" in note
    assert "no coverage at all" in note
    assert "Flock Camera net." in note
    assert "%Camera net.%" in note          # the documented widening step
    assert "Locally-administered (LAA) records:** none" in note


def test_coverage_attributes_laa_records_to_their_prefix():
    from ssid_query import coverage_note

    note = coverage_note({
        "flock_camera_net_records": 5,
        "fs_ext_battery_records": 2,
        "laa_records": 23,
        "laa_by_oui": {"82:6B:F2": 23},
    })
    assert "82:6B:F2" in note and "23" in note
    assert "anti-fingerprinting" in note
    assert "zero records" not in note        # class present → no zero-coverage note


def test_render_coverage_md_includes_note_table_and_carry_over():
    from ssid_query import render_coverage_md

    md = render_coverage_md({
        "patterns": {"Flock%": 1234, "FS Ext Battery%": 0},
        "patterns_note": "Carried over from the previous query pass.",
        "dataset": {
            "flock_camera_net_records": 0,
            "fs_ext_battery_records": 0,
            "laa_records": 23,
            "laa_by_oui": {"82:6B:F2": 23},
        },
    })
    assert "### Camera-class coverage" in md
    assert "| `Flock%` | 1,234 |" in md
    assert "| `FS Ext Battery%` | 0 |" in md
    assert "Carried over from the previous query pass." in md
