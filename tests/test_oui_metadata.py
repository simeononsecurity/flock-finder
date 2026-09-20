"""Tests for scripts/oui_metadata.py and the canonical OUI dataset."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from oui_metadata import list_ouis, load_oui_metadata, write_oui_json  # noqa: E402
from validation import is_valid_oui  # noqa: E402


def test_load_oui_metadata_all_valid():
    entries = load_oui_metadata()
    assert entries, "expected at least one OUI entry"
    for e in entries:
        assert is_valid_oui(e["oui"]), f"invalid OUI slipped through: {e['oui']}"
        assert e["oui"] == e["oui"].upper()


def test_load_oui_metadata_no_duplicates():
    ouis = list_ouis()
    assert len(ouis) == len(set(ouis)), "duplicate OUI prefixes in dataset"


def test_write_oui_json_roundtrip(tmp_path):
    import json

    out = tmp_path / "flock_ouis.json"
    write_oui_json(json_path=out)
    payload = json.loads(out.read_text())
    assert payload["match_confidence"] == "suspected"
    assert payload["total"] == len(payload["ouis"])
    assert payload["total"] >= 1


# ─── Firmware-derived additions (dougborg/PR#39) ──────────────────────────────

# Flock Safety's own IEEE registration — the only prefix in the list that is
# assigned to Flock rather than to a chipset/contract manufacturer.
FLOCK_DIRECT_OUI = "B4:1E:52"

# FS Ext Battery accessory / battery-pack series (same upstream source).
FS_EXT_BATTERY_OUIS = [
    "04:0D:84",
    "F0:82:C0",
    "1C:34:F1",
    "38:5B:44",
    "94:34:69",
    "B4:E3:F9",
]


def test_canonical_list_includes_flock_direct_registration():
    assert FLOCK_DIRECT_OUI in list_ouis()


def test_canonical_list_includes_fs_ext_battery_series():
    ouis = list_ouis()
    missing = [oui for oui in FS_EXT_BATTERY_OUIS if oui not in ouis]
    assert not missing, f"FS Ext Battery OUIs missing from flock_ouis.csv: {missing}"


def test_additions_carry_provenance_and_notes():
    """Every new prefix must say where it came from and why it is listed."""
    by_oui = {entry["oui"]: entry for entry in load_oui_metadata()}
    for oui in [FLOCK_DIRECT_OUI, *FS_EXT_BATTERY_OUIS]:
        entry = by_oui[oui]
        assert entry["source"] == "dougborg/PR#39", oui
        assert entry["notes"], f"{oui} has no notes explaining why it is listed"


def test_fallback_oui_list_matches_canonical_csv():
    """
    wigle_query.FLOCK_OUIS_FALLBACK is only a safety net for a missing CSV, but
    it must not drift: a prefix present in the fallback and missing from the CSV
    (or vice versa) means a scan silently queries the wrong set.
    """
    from wigle_query import FLOCK_OUIS_FALLBACK

    assert sorted(FLOCK_OUIS_FALLBACK) == sorted(list_ouis())


def test_generic_qca9377_oui_is_not_queried():
    """
    00:03:7F is the generic Qualcomm Atheros QCA9377 chipset OUI (the radio used
    in Flock's MSM8953-generation cameras) but it is shared with a huge installed
    base of unrelated hardware, so it must never become a query prefix — see
    docs/DATA_POLICY.md section 7. Only the exact factory-default MACs are
    meaningful, and those are full 6-byte addresses rather than prefixes.
    """
    assert "00:03:7F" not in list_ouis()
