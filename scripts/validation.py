#!/usr/bin/env python3
"""
Flock Finder — Shared Validation & Data-Policy Helpers
======================================================
Pure, dependency-free helper functions used by the collector
(`wigle_query.py`) and the test suite.

Keeping these functions free of any network / filesystem side effects means
they can be unit-tested quickly and reused for input validation, output
validation, and the public-data-precision policy.

IMPORTANT — Data policy:
    Every record produced by this project is a *SUSPECTED* Flock Safety
    device, inferred purely from a WiFi OUI (MAC prefix) match against
    crowdsourced WiGLE data. An OUI match is not proof: OUIs can be shared,
    reassigned, spoofed, or belong to unrelated hardware. Downstream code
    should always label these as "suspected", never as confirmed.
"""

from __future__ import annotations

import re

# ─── Constants ────────────────────────────────────────────────────────────────

# Confidence label applied to every emitted record. An OUI match is a
# heuristic, not a confirmation.
MATCH_CONFIDENCE = "suspected"

# Public coordinate precision. WiGLE trilateration is already approximate;
# we deliberately truncate published coordinates so the map communicates a
# neighborhood-level area rather than implying a precise, surveyable point.
# 3 decimal places ≈ 111 m at the equator.
PUBLIC_COORD_PRECISION = 3

# OUI prefix: three hex octets separated by colons, e.g. "70:C9:4E".
OUI_REGEX = re.compile(r"^[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}$")

# Full BSSID / MAC: six hex octets separated by colons.
MAC_REGEX = re.compile(
    r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"
)


# ─── OUI / MAC validation ─────────────────────────────────────────────────────

def is_valid_oui(oui: str) -> bool:
    """Return True if `oui` is a well-formed 3-octet OUI prefix."""
    return bool(oui) and bool(OUI_REGEX.match(oui.strip()))


def is_valid_mac(mac: str) -> bool:
    """Return True if `mac` is a well-formed 6-octet MAC/BSSID."""
    return bool(mac) and bool(MAC_REGEX.match(mac.strip()))


def normalize_oui(oui: str) -> str:
    """
    Normalize an OUI to canonical uppercase colon form (e.g. '70:C9:4E').

    Accepts input with surrounding whitespace or lowercase hex.
    Raises ValueError if the value is not a valid OUI prefix.
    """
    if not is_valid_oui(oui):
        raise ValueError(f"Invalid OUI prefix: {oui!r}")
    return oui.strip().upper()


def oui_from_netid(netid: str) -> str:
    """
    Extract the OUI prefix (first 3 octets) from a full BSSID/netid.

    Returns uppercase 'XX:XX:XX' or '' if the netid is too short/malformed.
    """
    if not netid:
        return ""
    parts = netid.strip().upper().split(":")
    if len(parts) < 3:
        return ""
    prefix = ":".join(parts[:3])
    return prefix if is_valid_oui(prefix) else ""


# ─── Coordinate validation & precision policy ─────────────────────────────────

def is_valid_latlon(lat, lon) -> bool:
    """
    Return True if lat/lon are real, in-range, and not the null-island (0,0).

    Rejects None, non-numeric, NaN, out-of-range, and the (0, 0) sentinel
    that frequently indicates a missing geocode.
    """
    try:
        latf = float(lat)
        lonf = float(lon)
    except (TypeError, ValueError):
        return False
    # NaN check (NaN != NaN)
    if latf != latf or lonf != lonf:
        return False
    if not (-90.0 <= latf <= 90.0) or not (-180.0 <= lonf <= 180.0):
        return False
    if latf == 0.0 and lonf == 0.0:
        return False
    return True


def round_coord(value, precision: int = PUBLIC_COORD_PRECISION):
    """
    Round a single coordinate to the public precision.

    Returns None if the value is not numeric so callers can drop the record.
    """
    try:
        return round(float(value), precision)
    except (TypeError, ValueError):
        return None


def redact_coordinates(lat, lon, precision: int = PUBLIC_COORD_PRECISION):
    """
    Reduce coordinate precision for public output.

    Returns (lat, lon) rounded to `precision` decimals, or (None, None)
    if either value is invalid.
    """
    rlat = round_coord(lat, precision)
    rlon = round_coord(lon, precision)
    if rlat is None or rlon is None:
        return None, None
    return rlat, rlon


# ─── Output record validation ─────────────────────────────────────────────────

def validate_record(record: dict) -> bool:
    """
    Validate a normalized network record before it is written to output.

    A record is considered valid when:
      * it has a well-formed netid (BSSID)
      * it has valid, in-range coordinates
    Other fields are optional/best-effort and do not fail validation.
    """
    if not isinstance(record, dict):
        return False
    netid = record.get("netid", "")
    if not is_valid_mac(netid):
        return False
    if not is_valid_latlon(record.get("trilat"), record.get("trilong")):
        return False
    return True


def filter_valid_records(records):
    """
    Split an iterable of records into (valid, rejected) lists.

    Useful for logging how many records were dropped and why, while
    guaranteeing only clean data reaches the published outputs.
    """
    valid, rejected = [], []
    for rec in records:
        (valid if validate_record(rec) else rejected).append(rec)
    return valid, rejected
