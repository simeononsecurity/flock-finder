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
