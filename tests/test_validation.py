"""Unit tests for scripts/validation.py (pure functions, no network/FS)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_SSID_CONFIRMED,
    MATCH_CONFIDENCE,
    OUI_TIER_MFR,
    annotate_record,
    classify_confidence,
    filter_valid_records,
    is_actionable_confidence,
    is_out_of_market,
    is_valid_latlon,
    is_valid_mac,
    is_valid_oui,
    is_valid_tier,
    normalize_oui,
    oui_from_netid,
    ssid_confirms_flock,
    ssid_denylist_match,
    summarize_confidence,
    validate_record,
)

# ─── OUI / MAC validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["70:C9:4E", "70:c9:4e", "00:f4:8d", "AB:CD:EF"])
def test_is_valid_oui_true(value):
    assert is_valid_oui(value)


@pytest.mark.parametrize("value", ["", None, "70:C9", "70:C9:4E:12", "ZZ:00:11", "70-C9-4E"])
def test_is_valid_oui_false(value):
    assert not is_valid_oui(value)


def test_normalize_oui_uppercases_and_strips():
    assert normalize_oui("  70:c9:4e ") == "70:C9:4E"


def test_normalize_oui_rejects_bad():
    with pytest.raises(ValueError):
        normalize_oui("nope")


@pytest.mark.parametrize("value", ["70:C9:4E:12:34:56", "00:f4:8d:aa:bb:cc"])
def test_is_valid_mac_true(value):
    assert is_valid_mac(value)


@pytest.mark.parametrize("value", ["", None, "70:C9:4E", "70:C9:4E:12:34", "gg:hh:ii:jj:kk:ll"])
def test_is_valid_mac_false(value):
    assert not is_valid_mac(value)


def test_oui_from_netid():
    assert oui_from_netid("70:C9:4E:12:34:56") == "70:C9:4E"
    assert oui_from_netid("70:c9:4e:12:34:56") == "70:C9:4E"
    assert oui_from_netid("bad") == ""
    assert oui_from_netid("") == ""


# ─── Coordinate validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("lat,lon", [(39.1, -94.5), (-89.9, 179.9), ("12.3", "45.6")])
def test_is_valid_latlon_true(lat, lon):
    assert is_valid_latlon(lat, lon)


@pytest.mark.parametrize(
    "lat,lon",
    [
        (0, 0),               # null island
        (None, 10),
        (10, None),
        (91, 10),             # lat out of range
        (10, 181),            # lon out of range
        ("x", 10),
        (float("nan"), 10),
    ],
)
def test_is_valid_latlon_false(lat, lon):
    assert not is_valid_latlon(lat, lon)


def test_full_precision_coords_are_valid():
    # Full-precision coordinates must remain valid — we never truncate.
    assert is_valid_latlon(39.123456789, -94.987654321)


# ─── Record validation ────────────────────────────────────────────────────────

def _rec(**over):
    base = {"netid": "70:C9:4E:12:34:56", "trilat": 39.1, "trilong": -94.5}
    base.update(over)
    return base


def test_validate_record_ok():
    assert validate_record(_rec())


def test_validate_record_preserves_full_precision_input():
    # validate_record only checks validity; it must accept full-precision coords.
    assert validate_record(_rec(trilat=39.123456789, trilong=-94.987654321))


def test_validate_record_bad_netid():
    assert not validate_record(_rec(netid="70:C9:4E"))  # OUI only, not full MAC


def test_validate_record_bad_coords():
    assert not validate_record(_rec(trilat=0, trilong=0))
    assert not validate_record(_rec(trilat=None))


def test_validate_record_non_dict():
    assert not validate_record("nope")


def test_filter_valid_records_splits():
    valid, rejected = filter_valid_records([
        _rec(),
        _rec(netid="bad"),
        _rec(trilat=None),
    ])
    assert len(valid) == 1
    assert len(rejected) == 2


def test_match_confidence_is_suspected():
    assert MATCH_CONFIDENCE == "suspected"


# ─── Evidence-tier classification ─────────────────────────────────────────────

def test_ssid_denylist_proves_other_hardware():
    assert ssid_denylist_match("ClickShare-Boardroom") == "clickshare"
    assert ssid_denylist_match("SMARTGATE_712638") == "smartgate_"
    assert ssid_denylist_match("DIRECT-4a-HP Printer") == "direct-"
    assert ssid_denylist_match("AndroidAP_1234") == "androidap"
    assert ssid_denylist_match("") == ""
    assert ssid_denylist_match("Flock Camera net.") == ""


def test_denylist_beats_confirm_patterns_on_flock_alpr_tool_text():
    """
    "Flock ALPR [wifi_receiver_oui;low]" is wardriving-tool verdict text stored in
    the SSID field, not a camera broadcast. It contains "flock", so the denylist
    must be consulted first or these records inflate the SSID-confirmed count.
    """
    tool_text = "Flock ALPR [wifi_receiver_oui;low]"
    assert ssid_denylist_match(tool_text) == "flock alpr ["
    assert not ssid_confirms_flock(tool_text)
    assert classify_confidence(tool_text) == CONFIDENCE_IDENTIFIED_OTHER


def test_ssid_confirms_flock_patterns():
    for ssid in ("Flock", "Flock-7EBB9D", "FLOCK-215CB4", "Flock Camera net.",
                 "FS Ext Battery", "fs ext battery 1234"):
        assert ssid_confirms_flock(ssid), ssid
        assert classify_confidence(ssid) == CONFIDENCE_SSID_CONFIRMED
    for ssid in ("", "ClickShare", "Home WiFi", "TEST"):
        assert not ssid_confirms_flock(ssid), ssid


def test_classify_confidence_tiers():
    assert classify_confidence("", "high") == CONFIDENCE_OUI_HIGH
    assert classify_confidence("", "mfr") == CONFIDENCE_OUI_MFR
    # A Flock SSID outranks the OUI tier it sits on.
    assert classify_confidence("Flock", "mfr") == CONFIDENCE_SSID_CONFIRMED
    # An unknown/missing tier must never silently downgrade to mfr.
    assert classify_confidence("", "bogus") == CONFIDENCE_OUI_HIGH
    assert classify_confidence("", None) == CONFIDENCE_OUI_HIGH


def test_is_valid_tier():
    assert is_valid_tier("high") and is_valid_tier("mfr")
    assert not is_valid_tier("HIGH") and not is_valid_tier("")


def test_out_of_market_flagging():
    assert not is_out_of_market("US")
    assert not is_out_of_market("us")
    for country in ("DE", "CA", "GB", "", None):
        assert is_out_of_market(country), country


def test_is_actionable_confidence_excludes_only_other_hardware():
    assert is_actionable_confidence(CONFIDENCE_SSID_CONFIRMED)
    assert is_actionable_confidence(CONFIDENCE_OUI_HIGH)
    assert is_actionable_confidence(CONFIDENCE_OUI_MFR)
    assert not is_actionable_confidence(CONFIDENCE_IDENTIFIED_OTHER)


def test_annotate_record_stamps_all_three_fields():
    rec = {"ssid": "ClickShare-1", "oui_match": "74:4c:a1", "country": "DE"}
    annotate_record(rec, {"74:4C:A1": OUI_TIER_MFR})
    assert rec["oui_tier"] == OUI_TIER_MFR
    assert rec["confidence"] == CONFIDENCE_IDENTIFIED_OTHER
    assert rec["out_of_market"] is True


def test_annotate_record_recomputes_a_stale_confidence():
    """A record loaded back from an older scan must be re-classified, not trusted."""
    rec = {"ssid": "Flock", "oui_match": "70:C9:4E", "country": "US",
           "confidence": "identified_other", "oui_tier": "mfr"}
    annotate_record(rec, {"70:C9:4E": "high"})
    assert rec["confidence"] == CONFIDENCE_SSID_CONFIRMED
    assert rec["oui_tier"] == "high"


def test_summarize_confidence_counts_and_totals():
    records = [
        {"ssid": "Flock", "oui_match": "70:C9:4E", "country": "US"},
        {"ssid": "", "oui_match": "70:C9:4E", "country": "US"},
        {"ssid": "", "oui_match": "F4:6A:DD", "country": "US"},
        {"ssid": "ClickShare-1", "oui_match": "F4:6A:DD", "country": "DE"},
        {"ssid": "Flock ALPR [x;low]", "oui_match": "70:C9:4E", "country": "US"},
    ]
    tiers = {"70:C9:4E": "high", "F4:6A:DD": "mfr"}
    summary = summarize_confidence(
        [annotate_record(dict(r), tiers) for r in records]
    )
    assert summary["total"] == 5
    assert summary["by_confidence"] == {
        CONFIDENCE_SSID_CONFIRMED: 1,
        CONFIDENCE_OUI_HIGH: 1,
        CONFIDENCE_OUI_MFR: 1,
        CONFIDENCE_IDENTIFIED_OTHER: 2,
    }
    assert summary["by_oui_tier"] == {"high": 3, "mfr": 2}
    assert summary["actionable"] == 3
    assert summary["out_of_market"] == 1
    assert summary["actionable_out_of_market"] == 0
    assert summary["ssid_denylist_hits"] == {"clickshare": 1, "flock alpr [": 1}
    assert summary["countries"] == 2


def test_summarize_confidence_accepts_raw_records():
    """Records that were never annotated are classified on the fly."""
    summary = summarize_confidence([
        {"ssid": "ClickShare", "oui_match": "74:4C:A1", "country": "US"},
        {"ssid": "", "oui_match": "70:C9:4E", "country": "US"},
    ])
    assert summary["by_confidence"][CONFIDENCE_IDENTIFIED_OTHER] == 1
    assert summary["by_confidence"][CONFIDENCE_OUI_HIGH] == 1
