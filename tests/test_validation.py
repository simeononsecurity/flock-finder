"""Unit tests for scripts/validation.py (pure functions, no network/FS)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from validation import (  # noqa: E402
    MATCH_CONFIDENCE,
    filter_valid_records,
    is_valid_latlon,
    is_valid_mac,
    is_valid_oui,
    normalize_oui,
    oui_from_netid,
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
