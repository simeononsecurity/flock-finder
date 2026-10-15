#!/usr/bin/env python3
"""
Flock Finder — Shared Validation & Data-Policy Helpers
======================================================
Pure, dependency-free helper functions used by the collector
(`wigle_query.py`) and the test suite.

Keeping these functions free of any network / filesystem side effects means
they can be unit-tested quickly and reused for input validation and output
validation.

IMPORTANT — Data policy:
    * Every record produced by this project is a *SUSPECTED* Flock Safety
      device, inferred purely from a WiFi OUI (MAC prefix) match against
      crowdsourced WiGLE data. An OUI match is not proof.
    * Coordinates are published at FULL precision and are NEVER modified /
      truncated — accuracy matters for mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable, Mapping

# ─── Constants ────────────────────────────────────────────────────────────────

# Confidence label applied to every emitted record. An OUI match is a
# heuristic, not a confirmation.
MATCH_CONFIDENCE = "suspected"

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


# ─── Coordinate validation ────────────────────────────────────────────────────

def is_valid_latlon(lat, lon) -> bool:
    """
    Return True if lat/lon are real, in-range, and not the null-island (0,0).

    Rejects None, non-numeric, NaN, out-of-range, and the (0, 0) sentinel
    that frequently indicates a missing geocode.

    NOTE: This only *validates* coordinates — it never modifies them. Published
    coordinates are always kept at full precision.
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


# ─── Confidence classification (per-feature evidence tiers) ───────────────────
#
# `match_confidence` above answers "is this an OUI match?" — always "suspected".
# The labels below answer the sharper question consumers actually need: *how
# strong is the evidence for this particular record?*
#
# The tiers exist because a bare OUI match is weak. The headline used to present
# every OUI hit as an equal "camera", when the dataset really holds three very
# different populations — Flock-named SSIDs (strong), unprefixed OUI hits
# (unverified), and records whose own SSID proves they are something else
# entirely (a ClickShare unit, a phone hotspot, a gateway AP). Publishing the
# tier per feature lets the map, the README headline and downstream consumers
# filter on evidence instead of trusting a single aggregate number.

CONFIDENCE_SSID_CORRELATED = "ssid_correlated"
# Read compatibility for older callers and released CSV files. New output must
# use ``ssid_correlated`` because an SSID pattern is correlation, not proof.
CONFIDENCE_SSID_CONFIRMED = CONFIDENCE_SSID_CORRELATED
LEGACY_CONFIDENCE_SSID_CONFIRMED = "ssid_confirmed"
CONFIDENCE_OUI_HIGH = "oui_high"
CONFIDENCE_OUI_MFR = "oui_mfr"
CONFIDENCE_IDENTIFIED_OTHER = "identified_other"

# Strongest → weakest. `unconfirmed_candidate` (candidate OUIs from the
# SSID-discovery pass) is a different axis; it lives in ssid_query.py.
CONFIDENCE_LEVELS = (
    CONFIDENCE_SSID_CORRELATED,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
    CONFIDENCE_IDENTIFIED_OTHER,
)

# Tiers that represent a plausible Flock device — everything except a record
# whose own SSID contradicts the match. This is the number the headline reports.
CONFIDENCE_ACTIONABLE = (
    CONFIDENCE_SSID_CORRELATED,
    CONFIDENCE_OUI_HIGH,
    CONFIDENCE_OUI_MFR,
)

CONFIDENCE_DESCRIPTIONS = {
    CONFIDENCE_SSID_CORRELATED: (
        "The SSID itself is a Flock naming pattern (e.g. 'Flock', 'Flock-XXXXXX', "
        "'FS Ext Battery'). Strongest evidence available from passive WiFi data."
    ),
    CONFIDENCE_OUI_HIGH: (
        "OUI prefix is on the high-confidence Flock list; the SSID is absent, "
        "hidden, or unrecognised. Suspected, unverified."
    ),
    CONFIDENCE_OUI_MFR: (
        "OUI prefix belongs to a contract manufacturer (Liteon/USI) that also "
        "ships unrelated hardware. Weakest OUI evidence — expect false positives."
    ),
    CONFIDENCE_IDENTIFIED_OTHER: (
        "The SSID positively identifies other hardware (ClickShare, SMARTGATE_, "
        "DIRECT-, AndroidAP, or wardriving-tool output), so the OUI match is "
        "spurious. Suppressed from public point artifacts and camera counts."
    ),
}


# Anchored patterns for documented SSID families. Broad substring matching such
# as ``"flock" in ssid`` turns unrelated names like "Bird Flock Guest" into
# positive evidence. Every expression below matches the full normalized SSID.
SSID_CORRELATION_RULES = (
    ("flock_base", r"^\s*flock\s*$", "documented base SSID"),
    ("flock_device_id", r"^\s*flock(?:-|_)?[0-9a-f]{3,12}\s*$",
     "documented Flock SSID with a hexadecimal device suffix"),
    ("flock_camera_net", r"^\s*flock\s+camera\s+net\.?\s*$",
     "documented camera-network SSID"),
    ("fs_ext_battery", r"^\s*fs\s+ext\s+battery(?:\s+[0-9a-z_-]+)?\s*$",
     "documented external-battery SSID family"),
)

# Narrow compatibility for metadata consumers which previously received a list
# of plain substrings. Values are regexes now and must be treated as such.
SSID_CONFIRM_PATTERNS = tuple(regex for _label, regex, _why in SSID_CORRELATION_RULES)
_SSID_CORRELATION_REGEXES = tuple(
    (label, re.compile(regex, re.IGNORECASE))
    for label, regex, _why in SSID_CORRELATION_RULES
)

# SSID rules that *contradict* a Flock match — the record is provably some other
# device that happens to share the OUI space. Checked BEFORE the confirm
# patterns, which is what the last rule depends on.
#
# Each rule is (label, regex, why). Regexes (case-insensitive, matched against
# the whole SSID) rather than plain substrings, because the detector-verdict rule
# has to catch every spelling variant of another tool's output, not one literal
# prefix — see the evidence on that rule below.
SSID_DENYLIST_RULES = (
    ("clickshare", r"clickshare",
     "Barco ClickShare wireless presentation units"),
    ("smartgate_", r"smartgate_",
     "SMARTGATE_###### gateway / intercom APs"),
    ("direct-", r"direct-",
     "DIRECT-xx consumer set-top / TV adapters"),
    ("androidap", r"androidap",
     "Android phone hotspots"),
    ("audi hud", r"audi\s*hud",
     "Audi head-up-display WiFi (car infotainment)"),
    ("max-printer", r"max[\s-]*printer",
     "MAX-PRINTER office printers"),
    # DETECTOR OUTPUT INGESTED AS DATA. Another tool's verdict string was written
    # into the SSID column and uploaded to WiGLE, so these records are not
    # observations of a camera at all — they are a downstream copy of a
    # detection. Observed variants (all 30 in Texas, 26 tagged "low" and 4
    # "medium" by the originating tool):
    #     Flock ALPR [flock_receiver_oui;low]
    #     Flock ALPR [wifi_receiver_oui;low]
    #     Flock ALPR [wifi_bssid_oui;low]
    #     Flock ALPR [wifi_hidden_ssid_oui;low]
    #     Flock ALPR [wifi_oui_wildcard_probe;medium]
    # No Flock camera broadcasts an SSID beginning "Flock ALPR", and they contain
    # "flock", so without this rule they would be counted as SSID-correlated —
    # i.e. the project would be scoring a detector's opinion as evidence.
    ("detector_verdict", r"^\s*flock[\s_-]*alpr\b",
     "another detector's verdict string stored in the SSID field"),
)

# Labels only, in rule order (back-compat for callers/tests that just want the
# set of reasons, and what the frontend publishes).
SSID_DENYLIST_PATTERNS = tuple(label for label, _regex, _why in SSID_DENYLIST_RULES)

SSID_DENYLIST_NOTES = {label: why for label, _regex, why in SSID_DENYLIST_RULES}

# Compiled once — classify_confidence() runs for every record on every scan.
_DENYLIST_REGEXES = tuple(
    (label, re.compile(regex, re.IGNORECASE)) for label, regex, _why in SSID_DENYLIST_RULES
)

# The verdict tag a detector-verdict SSID carries, e.g. ";low" in
# "Flock ALPR [wifi_receiver_oui;low]". Reported in the headline breakdown so
# readers can see that most of these records were low-confidence guesses by the
# tool that produced them.
_VERDICT_TAG_RE = re.compile(r";\s*(low|medium|high)\b", re.IGNORECASE)

# Countries where Flock Safety is known to operate in volume. Records outside
# this set are *flagged* (`out_of_market`), never dropped: Flock has expanded
# internationally and this project will not hide evidence of that — it simply
# refuses to present out-of-market OUI matches as equally likely.
PRIMARY_MARKET_COUNTRIES = ("US",)

# OUI confidence tiers (mirrors the firmware's high/mfr split in flock-you-esp32).
OUI_TIER_HIGH = "high"
OUI_TIER_MFR = "mfr"
OUI_TIERS = (OUI_TIER_HIGH, OUI_TIER_MFR)
OUI_TIER_DEFAULT = OUI_TIER_HIGH


# ─── Confidence classification ────────────────────────────────────────────────

def is_valid_tier(tier: str) -> bool:
    """Return True if `tier` is a known OUI confidence tier ('high' | 'mfr')."""
    return tier in OUI_TIERS


def ssid_denylist_match(ssid: str) -> str:
    """
    Return the denylist *label* matched by `ssid`, or '' if none matched.

    Matching is case-insensitive and regex-based (see SSID_DENYLIST_RULES), so
    spelling variants of another tool's output cannot slip through.
    """
    if not ssid:
        return ""
    for label, regex in _DENYLIST_REGEXES:
        if regex.search(ssid):
            return label
    return ""


def ssid_tool_verdict(ssid: str) -> str:
    """
    Return the confidence tag a detector-verdict SSID carries, else ''.

    Only meaningful for records whose SSID is another tool's output, e.g.
    "Flock ALPR [wifi_receiver_oui;low]" -> "low". Published so the evidence can
    show that these records were mostly low-confidence guesses by the tool that
    produced them, not observations.
    """
    if ssid_denylist_match(ssid) != "detector_verdict":
        return ""
    match = _VERDICT_TAG_RE.search(ssid)
    return match.group(1).lower() if match else ""


def ssid_confirms_flock(ssid: str) -> bool:
    """Compatibility wrapper for :func:`ssid_correlates_flock`."""
    return ssid_correlates_flock(ssid)


def ssid_correlates_flock(ssid: str) -> bool:
    """Return True when an SSID matches one documented, anchored family."""
    if not ssid:
        return False
    if ssid_denylist_match(ssid):
        return False
    return any(regex.fullmatch(ssid) for _label, regex in _SSID_CORRELATION_REGEXES)


def classify_confidence(ssid: str, oui_tier: str = OUI_TIER_DEFAULT) -> str:
    """
    Classify one record's evidence into a CONFIDENCE_* label.

    `oui_tier` is the tier of the matched OUI prefix ('high' or 'mfr'); an
    unknown tier falls back to 'high' so a missing or typo'd tier column can
    never silently downgrade the whole dataset.
    """
    if ssid_denylist_match(ssid):
        return CONFIDENCE_IDENTIFIED_OTHER
    if ssid_correlates_flock(ssid):
        return CONFIDENCE_SSID_CORRELATED
    return CONFIDENCE_OUI_MFR if oui_tier == OUI_TIER_MFR else CONFIDENCE_OUI_HIGH


def is_out_of_market(country: str) -> bool:
    """
    Return True when `country` is outside the primary Flock market (see
    PRIMARY_MARKET_COUNTRIES). Unknown/blank countries count as out-of-market
    rather than being assumed in-market.
    """
    return (country or "").strip().upper() not in PRIMARY_MARKET_COUNTRIES


def is_actionable_confidence(confidence: str) -> bool:
    """Return True for confidence tiers that represent a plausible Flock device."""
    return confidence in CONFIDENCE_ACTIONABLE


# ─── Confidence summaries ─────────────────────────────────────────────────────

def annotate_record(record: dict, oui_tier_map: dict = None) -> dict:
    """
    Add the published `confidence` / `oui_tier` / `out_of_market` fields to one
    record, in place, and return it.

    Centralised here so the collector, the per-OUI writers and any maintenance
    script produce byte-identical annotations. A record that already carries a
    `confidence` (e.g. one loaded back from a previous scan) keeps it only if it
    is a known label — otherwise it is recomputed, so a stale value can never
    survive a re-scan.
    """
    oui = (record.get("oui_match") or record.get("oui") or "").upper()
    tier = ""
    if oui_tier_map:
        tier = oui_tier_map.get(oui, "")
    if not is_valid_tier(tier):
        tier = record.get("oui_tier") if is_valid_tier(record.get("oui_tier")) else ""
    if not tier:
        tier = OUI_TIER_DEFAULT

    record["oui_tier"] = tier
    record["confidence"] = classify_confidence(record.get("ssid") or "", tier)
    record["out_of_market"] = is_out_of_market(record.get("country"))
    # Why the record was excluded from the map, when it was — so a reader (or a
    # filter) can tell "a ClickShare dongle" from "another tool's output" without
    # re-deriving it. Empty string for anything that is not identified_other.
    record["blocked_reason"] = ssid_denylist_match(record.get("ssid") or "")
    return record


def summarize_confidence(records) -> dict:
    """
    Aggregate confidence/market counts for an iterable of records.

    Each record is a mapping with 'ssid', 'oui_match' (or 'oui'), 'country' and,
    optionally, a precomputed 'confidence' / 'oui_tier'. Precomputed values win,
    so a scan that already annotated its records is summarised without
    re-classifying them (see annotate_record).

    Returns a dict suitable for embedding in scan_stats.json and for rendering
    the README headline / website hero:

        {
          "total": 146526,
          "actionable": 85689,              # everything except identified_other
          "by_confidence": {...},           # one entry per CONFIDENCE_* label
          "by_oui_tier": {"high": ..., "mfr": ...},
          "out_of_market": 70708,
          "actionable_out_of_market": 28914,
          "ssid_denylist_hits": {"clickshare": 50278, ...},
          "ssid_tool_verdicts": {"low": 26, "medium": 4},   # detector output only
          "by_oui_confidence": {"70:C9:4E": {"oui_high": 362, ...}, ...},
          "countries": 138,
          "regions": 50,
        }
    """
    by_confidence = {label: 0 for label in CONFIDENCE_LEVELS}
    by_oui_tier = {tier: 0 for tier in OUI_TIERS}
    denylist_hits = {pattern: 0 for pattern in SSID_DENYLIST_PATTERNS}
    tool_verdicts = {}
    by_oui_confidence = {}
    out_of_market = 0
    actionable_out_of_market = 0
    countries, regions = set(), set()
    total = 0

    for record in records:
        if not isinstance(record, dict):
            continue
        total += 1

        ssid = record.get("ssid") or ""
        oui = (record.get("oui_match") or record.get("oui") or "").upper()
        tier = record.get("oui_tier") or OUI_TIER_DEFAULT
        if not is_valid_tier(tier):
            tier = OUI_TIER_DEFAULT

        confidence = record.get("confidence")
        if confidence == LEGACY_CONFIDENCE_SSID_CONFIRMED:
            confidence = CONFIDENCE_SSID_CORRELATED
        if confidence not in CONFIDENCE_LEVELS:
            confidence = classify_confidence(ssid, tier)

        by_confidence[confidence] += 1
        by_oui_tier[tier] += 1

        # Per-prefix breakdown: the audit's point is that prefixes differ wildly in
        # signal-to-noise (one prefix's records are 70% ClickShare, another's are
        # almost all Flock-SSID), so publish the measurement instead of leaving it
        # for a reader to re-derive from the raw dataset.
        if oui:
            bucket = by_oui_confidence.setdefault(oui, {})
            bucket[confidence] = bucket.get(confidence, 0) + 1

        matched = ssid_denylist_match(ssid)
        if matched:
            denylist_hits[matched] += 1
            if matched == "detector_verdict":
                tag = ssid_tool_verdict(ssid) or "untagged"
                tool_verdicts[tag] = tool_verdicts.get(tag, 0) + 1

        country = (record.get("country") or "").strip().upper()
        if country:
            countries.add(country)
        region = (record.get("region") or "").strip()
        if region:
            regions.add(region)

        if is_out_of_market(country):
            out_of_market += 1
            if is_actionable_confidence(confidence):
                actionable_out_of_market += 1

    return {
        "total": total,
        "actionable": total - by_confidence[CONFIDENCE_IDENTIFIED_OTHER],
        "by_confidence": dict(by_confidence),
        "by_oui_tier": dict(by_oui_tier),
        "by_oui_confidence": {oui: dict(counts) for oui, counts in sorted(by_oui_confidence.items())},
        "out_of_market": out_of_market,
        "actionable_out_of_market": actionable_out_of_market,
        "ssid_denylist_hits": {
            pattern: hits for pattern, hits in denylist_hits.items() if hits
        },
        "ssid_tool_verdicts": tool_verdicts,
        "countries": len(countries),
        "regions": len(regions),
    }


# ─── Suppression registry ───────────────────────────────────────────────

SUPPRESSION_REGISTRY_VERSION = 1
SUPPRESSION_HASH_ALGORITHM = "sha256"


def netid_fingerprint(netid: str) -> str:
    """Return a stable, non-reversible key for a valid BSSID."""
    if not is_valid_mac(netid):
        raise ValueError(f"Invalid BSSID for suppression registry: {netid!r}")
    normalized = netid.strip().upper().encode("ascii")
    return sha256(normalized).hexdigest()


def empty_suppression_registry() -> dict:
    """Return a new suppression registry with no device identifiers exposed."""
    return {
        "schema_version": SUPPRESSION_REGISTRY_VERSION,
        "hash_algorithm": SUPPRESSION_HASH_ALGORITHM,
        "entries": {},
    }


def validate_suppression_registry(registry: object) -> tuple[str, ...]:
    """Return validation errors. Invalid registries must fail closed on load."""
    errors = []
    if not isinstance(registry, dict):
        return ("registry must be an object",)
    if registry.get("schema_version") != SUPPRESSION_REGISTRY_VERSION:
        errors.append("unsupported schema_version")
    if registry.get("hash_algorithm") != SUPPRESSION_HASH_ALGORITHM:
        errors.append("unsupported hash_algorithm")
    entries = registry.get("entries")
    if not isinstance(entries, dict):
        errors.append("entries must be an object")
        return tuple(errors)
    for fingerprint, metadata in entries.items():
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            errors.append(f"invalid fingerprint: {fingerprint!r}")
        if not isinstance(metadata, dict) or not metadata.get("reason"):
            errors.append(f"missing suppression reason: {fingerprint!r}")
    return tuple(errors)


def suppress_records(
    records: Mapping[str, dict],
    registry: dict,
    observed_at: str,
) -> tuple[dict, dict, dict]:
    """Remove suppressed points and update the hashed durable registry.

    The returned counts describe records excluded from this candidate release.
    Registry entries protect later incremental merges when a newer observation
    hides or renames the SSID which originally proved the device unrelated.
    """
    errors = validate_suppression_registry(registry)
    if errors:
        raise ValueError("invalid suppression registry: " + "; ".join(errors))

    updated = {
        **registry,
        "entries": {key: dict(value) for key, value in registry["entries"].items()},
    }
    public = {}
    by_reason: dict[str, int] = {}
    new_entries = 0

    for key, record in records.items():
        netid = record.get("netid") or key
        fingerprint = netid_fingerprint(netid)
        reason = ssid_denylist_match(record.get("ssid") or "")
        prior = updated["entries"].get(fingerprint)
        if reason or prior:
            reason = reason or prior["reason"]
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if prior is None:
                new_entries += 1
                prior = {"reason": reason, "first_suppressed": observed_at}
            prior["reason"] = reason
            prior["last_seen"] = observed_at
            updated["entries"][fingerprint] = prior
            continue
        public[key] = record

    updated["updated"] = observed_at
    updated["entry_count"] = len(updated["entries"])
    updated["counts_by_reason"] = {}
    for metadata in updated["entries"].values():
        reason = metadata["reason"]
        updated["counts_by_reason"][reason] = updated["counts_by_reason"].get(reason, 0) + 1
    summary = {
        "excluded_from_release": sum(by_reason.values()),
        "new_registry_entries": new_entries,
        "by_reason": dict(sorted(by_reason.items())),
        "registry_entries": len(updated["entries"]),
        "registry_by_reason": dict(sorted(updated["counts_by_reason"].items())),
    }
    return public, updated, summary


# ─── Release validation ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ReleaseValidationResult:
    """Fail-closed validation result for a candidate public dataset."""

    valid: bool
    errors: tuple[str, ...]
    record_count: int
    complete_ouis: int
    expected_ouis: int


def validate_release_candidate(
    records: Mapping[str, dict],
    expected_ouis: Iterable[str],
    pagination_outcomes: Mapping[str, object],
    baseline_count: int | None = None,
    minimum_record_ratio: float = 0.90,
) -> ReleaseValidationResult:
    """Validate a candidate release and reject any incomplete scan.

    Outcome values may be status strings, mappings with ``status``, or objects
    exposing a ``status`` attribute. Only the literal ``complete`` passes.
    """
    errors = []
    expected = tuple(dict.fromkeys(normalize_oui(oui) for oui in expected_ouis))
    complete = 0
    for oui in expected:
        raw = pagination_outcomes.get(oui)
        status = raw if isinstance(raw, str) else (
            raw.get("status") if isinstance(raw, Mapping) else getattr(raw, "status", None)
        )
        status = getattr(status, "value", status)
        if status == "complete":
            complete += 1
        else:
            errors.append(f"{oui}: pagination outcome is {status or 'missing'}")

    if expected and not records:
        errors.append("candidate dataset is empty")

    seen = set()
    for key, record in records.items():
        if not validate_record(record):
            errors.append(f"invalid record: {key}")
            continue
        netid = record["netid"].upper()
        if netid in seen:
            errors.append(f"duplicate netid: {netid}")
        seen.add(netid)

    if not 0 < minimum_record_ratio <= 1:
        errors.append("minimum_record_ratio must be greater than 0 and at most 1")
    elif baseline_count is not None and baseline_count > 0:
        floor = int(baseline_count * minimum_record_ratio)
        if len(records) < floor:
            errors.append(
                f"candidate record count {len(records)} is below release floor {floor} "
                f"from baseline {baseline_count}"
            )

    return ReleaseValidationResult(
        valid=not errors,
        errors=tuple(errors),
        record_count=len(records),
        complete_ouis=complete,
        expected_ouis=len(expected),
    )


# ─── Labeled classifier evaluation ────────────────────────────────────────────

def evaluate_labeled_samples(samples: Iterable[Mapping[str, str]]) -> dict:
    """Report SSID matcher precision and recall for each labeled prefix."""
    buckets: dict[str, dict] = {}
    for row in samples:
        prefix = normalize_oui(row.get("oui", ""))
        label = (row.get("label") or "").strip().lower()
        if label not in {"positive", "negative"}:
            raise ValueError(f"invalid fixture label for {prefix}: {label!r}")
        review_date = (row.get("review_date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date):
            raise ValueError(f"invalid review_date for {prefix}: {review_date!r}")
        bucket = buckets.setdefault(prefix, {
            "true_positive": 0, "false_positive": 0,
            "true_negative": 0, "false_negative": 0,
            "sample_size": 0, "review_date": review_date,
        })
        bucket["review_date"] = max(bucket["review_date"], review_date)
        predicted = ssid_correlates_flock(row.get("ssid") or "")
        actual = label == "positive"
        outcome = (
            "true_positive" if predicted and actual else
            "false_positive" if predicted else
            "false_negative" if actual else
            "true_negative"
        )
        bucket[outcome] += 1
        bucket["sample_size"] += 1

    for bucket in buckets.values():
        predicted_positive = bucket["true_positive"] + bucket["false_positive"]
        actual_positive = bucket["true_positive"] + bucket["false_negative"]
        bucket["precision"] = (
            bucket["true_positive"] / predicted_positive if predicted_positive else None
        )
        bucket["recall"] = (
            bucket["true_positive"] / actual_positive if actual_positive else None
        )
    return {prefix: buckets[prefix] for prefix in sorted(buckets)}


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
