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


def test_write_oui_json_does_not_churn_when_unchanged(tmp_path):
    """
    CI regenerates this file on every push; if it rewrote the `generated`
    timestamp unconditionally, every run would produce a diff for no change.
    """
    out = tmp_path / "flock_ouis.json"
    write_oui_json(json_path=out)
    first = out.read_text()

    assert write_oui_json(json_path=out) == out
    assert out.read_text() == first


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


# ─── Confidence tiers (mirrors the firmware's high/mfr split) ─────────────────

# Contract-manufacturer prefixes: Flock hardware, but the same silicon ships in
# unrelated products (Liteon Technology / Universal Scientific Industrial).
MFR_TIER_OUIS = {"F4:6A:DD", "F8:A2:D6", "00:F4:8D", "D0:39:57", "E8:D0:FC", "E0:0A:F6"}


def test_every_entry_has_a_valid_tier():
    for entry in load_oui_metadata():
        assert entry["tier"] in ("high", "mfr"), entry


def test_contract_manufacturer_prefixes_are_mfr_tier():
    by_oui = {entry["oui"]: entry["tier"] for entry in load_oui_metadata()}
    assert {oui for oui, tier in by_oui.items() if tier == "mfr"} == MFR_TIER_OUIS


def test_reconciles_with_the_firmware_list():
    """
    flock-you-esp32 carries 41 prefixes, this repo 39: the two deliberate
    omissions are 00:03:7F (generic Atheros — querying it floods WiGLE) and
    D4:11:D6 (SoundThinking, a different device class). E0:0A:F6 was the third
    delta and must be present.
    """
    ouis = set(list_ouis())
    assert "E0:0A:F6" in ouis
    assert "00:03:7F" not in ouis
    assert "D4:11:D6" not in ouis


def test_every_entry_has_a_vendor():
    for entry in load_oui_metadata():
        assert entry["vendor"], entry
    vendors = {entry["oui"]: entry["vendor"] for entry in load_oui_metadata()}
    assert vendors["B4:1E:52"] == "Flock Safety"
    assert vendors["E0:0A:F6"] == "Liteon Technology Corporation"
    assert vendors["A4:CF:12"] == "Espressif Inc."
    assert vendors["E0:4F:43"] == "Universal Global Scientific Industrial Co., Ltd."


def test_tier_map_agrees_with_the_metadata():
    from oui_metadata import oui_tier_map

    assert oui_tier_map() == {e["oui"]: e["tier"] for e in load_oui_metadata()}


def test_json_mirror_carries_tiers_and_classifier(tmp_path):
    import json

    from oui_metadata import write_oui_json

    out = tmp_path / "flock_ouis.json"
    write_oui_json(json_path=out)
    payload = json.loads(out.read_text())

    assert payload["total_by_tier"] == {"high": 33, "mfr": 6}
    assert all(entry["tier"] in ("high", "mfr") for entry in payload["ouis"])
    assert all(entry["vendor"] for entry in payload["ouis"])

    # The classifier block is what keeps the browser's rules identical to the
    # collector's — if it disappears the site silently mis-tiers every record.
    classifier = payload["classifier"]
    assert "flock" in classifier["ssid_confirm_patterns"]
    assert classifier["primary_market_countries"] == ["US"]
    assert classifier["confidence_levels"][0] == "ssid_confirmed"
    assert "identified_other" not in classifier["actionable_levels"]

    # Denylist rules must ship as regexes: the browser cannot robustly exclude
    # another tool's verdict strings from labels alone.
    labels = [rule["label"] for rule in classifier["ssid_denylist_rules"]]
    assert "detector_verdict" in labels
    for rule in classifier["ssid_denylist_rules"]:
        assert rule["regex"] and rule["why"]
    detector = next(r for r in classifier["ssid_denylist_rules"]
                    if r["label"] == "detector_verdict")
    assert "alpr" in detector["regex"].lower()
