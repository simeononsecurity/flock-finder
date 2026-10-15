#!/usr/bin/env python3
"""
Flock Finder — WiGLE Dataset Query
===================================
Queries the WiGLE WiFi database for networks matching known Flock Safety
ALPR camera OUI prefixes. Outputs GeoJSON + CSV for the web map.

Data is CUMULATIVE — each run merges new results with existing data.
Records older than 2 years (based on lasttime) are pruned automatically.

**Incremental scanning** — per-OUI state is tracked so that subsequent
runs only request data updated since the previous successful scan (with a
1-day overlap for safety).  Interrupted pagination (e.g. rate-limit) is
automatically resumed on the next run.

Based on research by @NitekryDPaul (promiscuous-mode OUI discovery) and
the DeFlock project (https://www.deflock.me).

Similar to: https://github.com/simeononsecurity/track-openroaming-passpoint

WiGLE API docs: https://api.wigle.net

Usage:
    python3 scripts/wigle_query.py                    # Full scan, all OUIs
    python3 scripts/wigle_query.py --oui 70:c9:4e     # Single OUI
    python3 scripts/wigle_query.py --bbox 37,-97,39,-94  # Bounding box
    python3 scripts/wigle_query.py --country US        # Country filter
    python3 scripts/wigle_query.py --dry-run           # Auth check only
    python3 scripts/wigle_query.py --full-rescan       # Ignore saved state
"""

import argparse
import csv
import io
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import requests
from dotenv import load_dotenv
from oui_metadata import oui_tier_map, write_oui_json

# Shared, unit-tested helpers (validation + public-data policy)
from validation import (
    MATCH_CONFIDENCE,
    annotate_record,
    empty_suppression_registry,
    is_valid_latlon,
    is_valid_mac,
    is_valid_oui,
    netid_fingerprint,
    normalize_oui,
    summarize_confidence,
    suppress_records,
    validate_release_candidate,
    validate_suppression_registry,
)

# ─── Configuration ────────────────────────────────────────────────────────────


SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
ENV_FILE = PROJECT_DIR / ".env"

# WiGLE API
WIGLE_API_BASE = "https://api.wigle.net/api/v2"
WIGLE_SEARCH_ENDPOINT = f"{WIGLE_API_BASE}/network/search"

# Rate limiting — WiGLE daily API limits are strict
RATE_LIMIT_DELAY = 2.0      # seconds between requests
PAGE_SIZE = 100              # WiGLE default page size
MAX_RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
RETRY_JITTER_RATIO = 0.25

# Data retention — discard records with lasttime older than this
MAX_AGE_DAYS = 730           # 2 years

# Safety overlap when doing incremental fetches — re-query 1 day before the
# last successful scan to avoid missing records that arrived between scans.
INCREMENTAL_OVERLAP_DAYS = 1

# Output files
GEOJSON_OUTPUT = DATA_DIR / "flock_cameras.geojson"
CSV_OUTPUT = DATA_DIR / "flock_cameras.csv"
STATS_OUTPUT = DATA_DIR / "scan_stats.json"
SCAN_STATE_FILE = DATA_DIR / "scan_state.json"
SUPPRESSION_REGISTRY_FILE = DATA_DIR / "suppression_registry.json"
REBUILD_POLICY_FILE = DATA_DIR / "rebuild_policy.json"
CLASSIFIER_REPORT_FILE = DATA_DIR / "validation" / "ssid_metrics.json"


class PaginationStatus(str, Enum):
    """Terminal outcome of one OUI pagination attempt."""

    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True)
class PaginationResult:
    records: list[dict]
    status: PaginationStatus
    search_after: str | None
    page: int
    error: str | None = None


def retry_delay(
    attempt: int,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    jitter_ratio: float = RETRY_JITTER_RATIO,
    random_value: float | None = None,
) -> float:
    """Return bounded exponential backoff with symmetric jitter."""
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    bounded = min(max_delay, base_delay * (2 ** (attempt - 1)))
    sample = random.random() if random_value is None else random_value
    jitter = bounded * jitter_ratio * ((sample * 2) - 1)
    return max(0.0, min(max_delay, bounded + jitter))


# ─── Atomic Writes (scan integrity) ──────────────────────────────────────────
# Write to a temp file in the same directory, then os.replace() into place.
# os.replace is atomic on the same filesystem, so a crash / kill / rate-limit
# mid-write can never leave a truncated or half-valid data file behind.

def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text to `path` via a temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    """Atomically write `obj` as indented JSON to `path`."""
    atomic_write_text(path, json.dumps(obj, indent=2) + "\n")


def load_suppression_registry(path: Path = None) -> dict:
    """Load the durable suppression registry and fail closed if malformed."""
    path = path or SUPPRESSION_REGISTRY_FILE
    if not path.exists():
        return empty_suppression_registry()
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not load suppression registry {path}: {exc}") from exc
    errors = validate_suppression_registry(registry)
    if errors:
        raise ValueError(f"invalid suppression registry {path}: {'; '.join(errors)}")
    return registry


def load_rebuild_policy(path: Path = None) -> dict:
    """Load and validate scheduled authoritative-rebuild policy."""
    path = path or REBUILD_POLICY_FILE
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not load rebuild policy {path}: {exc}") from exc
    interval = policy.get("authoritative_rebuild_interval_days")
    ratio = policy.get("minimum_release_record_ratio")
    if not isinstance(interval, int) or interval < 1:
        raise ValueError("authoritative_rebuild_interval_days must be a positive integer")
    if not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
        raise ValueError("minimum_release_record_ratio must be greater than 0 and at most 1")
    for key in ("minimum_fixture_precision", "minimum_fixture_recall"):
        floor = policy.get(key)
        if not isinstance(floor, (int, float)) or not 0 <= floor <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    return policy


def authoritative_rebuild_due(stats: dict, policy: dict, now: datetime = None) -> bool:
    """Return whether the configured authoritative rebuild interval elapsed."""
    now = now or datetime.now(timezone.utc)
    raw = stats.get("last_authoritative_rebuild")
    if not raw:
        return True
    try:
        completed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return now - completed >= timedelta(days=policy["authoritative_rebuild_interval_days"])


def select_authoritative_rebuild(
    forced: bool,
    scheduled: bool,
    stats: dict,
    policy: dict,
    now: datetime = None,
) -> bool:
    """Select rebuild mode without skipping a scheduled incremental scan."""
    return forced or (scheduled and authoritative_rebuild_due(stats, policy, now))



# ─── Flock Safety OUI List ────────────────────────────────────────────────────

def load_ouis(oui_file: Path = None) -> list:
    """
    Load and validate Flock Safety OUI prefixes from the canonical CSV.

    Malformed or duplicate rows reject the whole query set. Silently reducing
    the configured prefixes would make a partial scan look complete.
    """
    if oui_file is None:
        oui_file = DATA_DIR / "flock_ouis.csv"

    ouis = []
    seen = set()
    errors = []
    with open(oui_file, "r") as f:
        reader = csv.DictReader(f)
        for line_number, row in enumerate(reader, 2):
            raw = (row.get("oui") or "").strip()
            if not is_valid_oui(raw):
                errors.append(f"line {line_number}: malformed OUI {raw!r}")
                continue
            oui = normalize_oui(raw)
            if oui in seen:
                errors.append(f"line {line_number}: duplicate OUI {oui}")
                continue
            seen.add(oui)
            ouis.append(oui)
    if errors:
        raise ValueError("invalid OUI query list: " + "; ".join(errors))
    if not ouis:
        raise ValueError("invalid OUI query list: no OUI prefixes")
    return ouis



# All known Flock Safety OUI prefixes (fallback if the canonical CSV is missing).
# MUST be kept in sync with data/flock_ouis.csv — this list is only a safety net
# so a missing/unreadable CSV degrades to "scan the known prefixes" instead of
# "scan nothing". tests/test_oui_metadata.py asserts the two agree.
FLOCK_OUIS_FALLBACK = [
    "70:C9:4E", "3C:91:80", "D8:F3:BC", "80:30:49", "B8:35:32",
    "14:5A:FC", "74:4C:A1", "08:3A:88", "9C:2F:9D", "C0:35:32",
    "94:08:53", "E4:AA:EA", "F4:6A:DD", "F8:A2:D6", "24:B2:B9",
    "00:F4:8D", "D0:39:57", "E8:D0:FC", "E0:4F:43", "B8:1E:A4",
    "70:08:94", "58:8E:81", "EC:1B:BD", "3C:71:BF", "58:00:E3",
    "90:35:EA", "5C:93:A2", "64:6E:69", "48:27:EA", "A4:CF:12",
    "82:6B:F2",
    # Flock Safety direct IEEE assignment (dougborg/PR#39)
    "B4:1E:52",
    # FS Ext Battery device series (dougborg/PR#39)
    "04:0D:84", "F0:82:C0", "1C:34:F1", "38:5B:44", "94:34:69",
    "B4:E3:F9",
    # Contract-manufacturer silicon (dougborg/PR#39); mfr tier in the CSV
    "E0:0A:F6",
]


# ─── Scan State Persistence ──────────────────────────────────────────────────

def load_scan_state(state_path: Path = None) -> dict:
    """
    Load per-OUI scan state from disk.

    Returns a dict keyed by OUI prefix (uppercase), e.g.:
        {
            "70:C9:4E": {
                "last_completed": "2025-07-14T12:00:00+00:00",
                "status": "completed",       # completed | interrupted | pending_publish
                "search_after": null,         # pagination cursor (if interrupted)
                "page": 0,                    # page reached (if interrupted)
                "fetched_so_far": 0,          # results from interrupted scan
                "since_date": "20250713"      # lastupdt value used
            },
            ...
        }
    """
    if state_path is None:
        state_path = SCAN_STATE_FILE
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
        # The state file has a top-level "ouis" dict
        states = data.get("ouis")
        if not isinstance(states, dict):
            raise ValueError("missing ouis object")
        return states
    except Exception as exc:
        raise ValueError(f"could not load scan state {state_path}: {exc}") from exc


def save_scan_state(oui_states: dict, state_path: Path = None) -> None:
    """Persist per-OUI scan state to disk."""
    if state_path is None:
        state_path = SCAN_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "description": "Per-OUI incremental scan state for flock-finder. "
                       "Delete this file to force a full rescan.",
        "ouis": oui_states,
    }
    atomic_write_json(state_path, envelope)



def compute_since_date(oui_state: dict, max_age_days: int) -> str:
    """
    Determine the `lastupdt` filter date for a given OUI.

    - If the OUI was fully completed before, use (last_completed − overlap).
    - If the OUI scan was interrupted, reuse the same since_date that was in
      progress (so the resumed pagination stays consistent).
    - If no prior state, fall back to the full retention window.

    Returns date string in WiGLE format: 'YYYYMMDD'
    """
    if not oui_state:
        # First time — full retention window
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        return cutoff.strftime("%Y%m%d")

    status = oui_state.get("status", "")

    if status in {"interrupted", "pending_publish"}:
        # Resume: reuse the exact same since_date so pagination is consistent
        saved = oui_state.get("since_date", "")
        if saved:
            return saved
        # Fallback if missing for some reason
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        return cutoff.strftime("%Y%m%d")

    if status == "completed":
        last_done = oui_state.get("last_completed", "")
        if last_done:
            try:
                # Parse ISO format
                dt = datetime.fromisoformat(last_done)
                # Go back INCREMENTAL_OVERLAP_DAYS for safety
                since = dt - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
                # But never go past the full retention window
                floor = datetime.now(timezone.utc) - timedelta(days=max_age_days)
                if since < floor:
                    since = floor
                return since.strftime("%Y%m%d")
            except (ValueError, TypeError):
                pass

    # Fallback — full window
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return cutoff.strftime("%Y%m%d")


def sort_ouis_by_priority(ouis: list, oui_states: dict) -> list:
    """
    Sort OUI list so that interrupted scans come first (to finish them),
    then never-scanned OUIs, then completed OUIs sorted oldest-first.
    This maximises useful work under daily API limits.
    """
    def sort_key(oui):
        state = oui_states.get(oui.upper(), {})
        status = state.get("status", "never")
        if status == "interrupted":
            return (0, "")  # Highest priority — finish what we started
        elif status == "pending_publish":
            return (1, "")  # Pagination done; await the rest of the campaign
        elif status == "never" or not status:
            return (2, "")  # Next priority — never scanned
        else:
            # Completed — sort oldest first so stale data gets refreshed
            return (3, state.get("last_completed", ""))

    return sorted(ouis, key=sort_key)


# ─── WiGLE API Client ────────────────────────────────────────────────────────

class WiGLEClient:
    """Thin wrapper around the WiGLE v2 REST API."""

    def __init__(self, api_name: str, api_token: str):
        self.session = requests.Session()
        self.session.auth = (api_name, api_token)
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "flock-finder/1.0 (https://github.com/simeononsecurity/flock-finder)",
        })
        self.request_count = 0

    def verify_auth(self) -> bool:
        """Verify API credentials are valid."""
        try:
            resp = self.session.get(f"{WIGLE_API_BASE}/profile/user", timeout=15)
            if resp.status_code == 200:
                # NOTE: Deliberately do NOT print the WiGLE userid here — this
                # script runs in public GitHub Actions logs and the username
                # would otherwise be leaked. Just confirm auth succeeded.
                print("  [✓] Authenticated successfully")
                return True
            elif resp.status_code == 401:
                print("  [✗] Authentication failed — check API credentials in .env")
                return False
            else:
                print(f"  [!] Unexpected status {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as exc:
            print(f"  [✗] Connection error: {exc}")
            return False

    def search_wifi(self, netid: str = None, ssid: str = None,
                    latrange1: float = None, latrange2: float = None,
                    longrange1: float = None, longrange2: float = None,
                    country: str = None, search_after: str = None,
                    results_per_page: int = PAGE_SIZE,
                    lastupdt: str = None) -> dict:
        """
        Search WiGLE WiFi database.

        Args:
            netid: BSSID/MAC address pattern (e.g., "70:C9:4E" for OUI prefix)
            lastupdt: Only return results updated after this date (YYYYMMDD)
        """
        params = {"resultsPerPage": results_per_page}
        if netid:
            params["netid"] = netid
        if ssid:
            params["ssid"] = ssid
        if latrange1 is not None:
            params["latrange1"] = latrange1
        if latrange2 is not None:
            params["latrange2"] = latrange2
        if longrange1 is not None:
            params["longrange1"] = longrange1
        if longrange2 is not None:
            params["longrange2"] = longrange2
        if country:
            params["country"] = country
        if search_after:
            params["searchAfter"] = search_after
        if lastupdt:
            params["lastupdt"] = lastupdt

        self.request_count += 1
        try:
            resp = self.session.get(WIGLE_SEARCH_ENDPOINT, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                print("  [✗] 401 Unauthorized — API token may have expired")
                return {"success": False, "error": "unauthorized"}
            elif resp.status_code == 429:
                print("  [!] 429 Rate limited — daily API quota exceeded")
                return {
                    "success": False,
                    "error": "rate_limited",
                    "retry_after": resp.headers.get("Retry-After"),
                }
            elif resp.status_code == 404:
                # WiGLE returns 404 when no results match the search
                return {"success": True, "results": [], "totalResults": 0}
            elif 500 <= resp.status_code < 600:
                print(f"  [!] HTTP {resp.status_code}: transient server error")
                return {"success": False, "error": f"http_{resp.status_code}", "transient": True}
            else:
                print(f"  [!] HTTP {resp.status_code}: {resp.text[:200]}")
                return {"success": False, "error": f"http_{resp.status_code}"}
        except requests.Timeout:
            print("  [!] Request timed out")
            return {"success": False, "error": "timeout"}
        except requests.RequestException as exc:
            print(f"  [!] Transient request error: {exc}")
            return {"success": False, "error": "request_error", "transient": True}
        except Exception as exc:
            print(f"  [!] Request error: {exc}")
            return {"success": False, "error": str(exc)}


# ─── Query Logic ──────────────────────────────────────────────────────────────

def query_oui(client: WiGLEClient, oui: str, country: str = None,
              bbox: tuple = None, since_date: str = None,
              resume_search_after: str = None,
              resume_page: int = 0,
              max_attempts: int = MAX_RETRY_ATTEMPTS,
              sleep_fn=time.sleep,
              random_fn=random.random) -> PaginationResult:
    """
    Query WiGLE for ALL WiFi networks matching a given OUI prefix.
    Paginates through every page until no more results.

    Args:
        since_date: YYYYMMDD — only fetch records updated after this date
        resume_search_after: Pagination cursor to resume from (if resuming)
        resume_page: Page number to resume from (cosmetic, for logging)

    Returns an explicit complete, interrupted, or failed outcome. Only a
    complete outcome is eligible for a completed OUI checkpoint.
    """
    networks = []
    search_after = resume_search_after
    page = resume_page
    netid_pattern = oui.upper()

    while True:
        page += 1

        kwargs = {"netid": netid_pattern}
        if country:
            kwargs["country"] = country
        if bbox:
            kwargs["latrange1"] = bbox[0]
            kwargs["longrange1"] = bbox[1]
            kwargs["latrange2"] = bbox[2]
            kwargs["longrange2"] = bbox[3]
        if search_after:
            kwargs["search_after"] = search_after
        if since_date:
            kwargs["lastupdt"] = since_date

        result = None
        for attempt in range(1, max_attempts + 1):
            result = client.search_wifi(**kwargs)
            if result.get("success", False):
                break
            error = result.get("error", "unknown")
            if error == "rate_limited":
                print(f"    ⚠ Rate limited after {len(networks)} results; resume state retained")
                return PaginationResult(
                    networks, PaginationStatus.INTERRUPTED, search_after, page - 1, error
                )
            transient = bool(result.get("transient")) or error == "timeout"
            if not transient or attempt == max_attempts:
                return PaginationResult(
                    networks, PaginationStatus.FAILED, search_after, page - 1, error
                )
            delay = retry_delay(attempt, random_value=random_fn())
            print(f"    Retry {attempt}/{max_attempts - 1} in {delay:.1f}s after {error}")
            sleep_fn(delay)

        if result is None or not result.get("success", False):
            return PaginationResult(
                networks, PaginationStatus.FAILED, search_after, page - 1, "unknown"
            )

        results = result.get("results")
        if not isinstance(results, list):
            return PaginationResult(
                networks, PaginationStatus.FAILED, search_after, page - 1, "invalid_results"
            )
        if not results:
            return PaginationResult(networks, PaginationStatus.COMPLETE, None, page)

        for net in results:
            networks.append({
                "netid": net.get("netid", ""),
                "ssid": net.get("ssid", ""),
                "trilat": net.get("trilat"),
                "trilong": net.get("trilong"),
                "channel": net.get("channel"),
                "encryption": net.get("encryption", ""),
                "type": net.get("type", "wifi"),
                "firsttime": net.get("firsttime", ""),
                "lasttime": net.get("lasttime", ""),
                "city": net.get("city", ""),
                "region": net.get("region", ""),
                "country": net.get("country", ""),
                "housenumber": net.get("housenumber", ""),
                "road": net.get("road", ""),
                "postalcode": net.get("postalcode", ""),
                "oui_match": oui.upper(),
            })

        total_count = result.get("totalResults", result.get("resultCount", 0))
        print(f"    Page {page}: +{len(results)} results  (running: {len(networks)}, "
              f"API total: {total_count})")

        # Pagination — get next page token
        search_after = result.get("searchAfter")
        if len(results) < PAGE_SIZE:
            return PaginationResult(networks, PaginationStatus.COMPLETE, None, page)
        if not search_after:
            return PaginationResult(
                networks,
                PaginationStatus.FAILED,
                None,
                page,
                "missing_search_after_on_full_page",
            )

        # Rate limiting between pages
        sleep_fn(RATE_LIMIT_DELAY)


# ─── Cumulative Data Management ──────────────────────────────────────────────

def load_existing_data(geojson_path: Path) -> dict:
    """
    Load existing GeoJSON data from a previous scan.
    Returns dict keyed by netid (BSSID) for easy merging.
    """
    existing = {}
    if not geojson_path.exists():
        return existing

    try:
        with open(geojson_path, "r") as f:
            data = json.load(f)
        if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            raise ValueError("existing dataset is not a GeoJSON FeatureCollection")

        for feature in data.get("features", []):
            props = feature.get("properties", {})
            netid = props.get("netid", "").upper()
            if netid:
                coords = feature.get("geometry", {}).get("coordinates", [None, None])
                existing[netid] = {
                    "netid": props.get("netid", ""),
                    "ssid": props.get("ssid", ""),
                    "trilat": coords[1],
                    "trilong": coords[0],
                    "channel": props.get("channel"),
                    "encryption": props.get("encryption", ""),
                    "firsttime": props.get("firsttime", ""),
                    "lasttime": props.get("lasttime", ""),
                    "city": props.get("city", ""),
                    "region": props.get("region", ""),
                    "country": props.get("country", ""),
                    "road": props.get("road", ""),
                    "postalcode": props.get("postalcode", ""),
                    "oui_match": props.get("oui", ""),
                }

        print(f"  [✓] Loaded {len(existing)} existing records from previous scan")
    except Exception as exc:
        raise ValueError(f"could not load existing data {geojson_path}: {exc}") from exc

    return existing


def merge_networks(existing: dict, new_networks: list, suppression_registry: dict = None) -> dict:
    """
    Merge new scan results into existing data.
    For duplicate netids, keep the record with the most recent lasttime.
    """
    merged = dict(existing)  # Start with existing data

    updated = 0
    added = 0

    suppressed = (suppression_registry or {}).get("entries", {})
    skipped_suppressed = 0
    for net in new_networks:
        netid = net["netid"].upper()
        if is_valid_mac(netid) and netid_fingerprint(netid) in suppressed:
            skipped_suppressed += 1
            continue
        if netid in merged:
            # Update if new record has a more recent lasttime
            old_lt = merged[netid].get("lasttime", "")
            new_lt = net.get("lasttime", "")
            if new_lt >= old_lt:
                merged[netid] = net
                updated += 1
        else:
            merged[netid] = net
            added += 1

    print(f"  Merge: +{added} new, ~{updated} updated, {len(merged)} total")
    if skipped_suppressed:
        print(f"  Merge: kept {skipped_suppressed} registry-suppressed record(s) out")
    return merged


def prune_old_records(records: dict, max_age_days: int = MAX_AGE_DAYS) -> dict:
    """
    Remove records with lasttime older than max_age_days.
    WiGLE lasttime format: "2024-01-15T12:00:00.000"
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    pruned = {}
    removed = 0

    for netid, net in records.items():
        lasttime = net.get("lasttime", "")
        # Parse various date formats
        if lasttime:
            date_part = lasttime[:10]  # "YYYY-MM-DD"
            if date_part >= cutoff_str:
                pruned[netid] = net
            else:
                removed += 1
        else:
            # Keep records with no lasttime (can't determine age)
            pruned[netid] = net

    if removed > 0:
        print(f"  Pruned {removed} records older than {max_age_days} days "
              f"(before {cutoff_str})")
    else:
        print(f"  No records older than {max_age_days} days to prune")

    return pruned


# ─── Output Generators ───────────────────────────────────────────────────────

def dedup_by_netid(records: dict) -> dict:
    """
    Final deduplication pass — guarantee exactly one record per netid (BSSID).
    Keeps the record with the most recent lasttime.

    This runs as a safety net after merge to ensure no duplicates survive
    from any source (API returning dupes, case-sensitivity, etc.).
    """
    clean = {}
    dupes = 0

    for netid, net in records.items():
        # Normalize key to uppercase
        key = netid.upper()
        if key in clean:
            dupes += 1
            old_lt = clean[key].get("lasttime", "")
            new_lt = net.get("lasttime", "")
            if new_lt >= old_lt:
                clean[key] = net
        else:
            clean[key] = net

    if dupes > 0:
        print(f"  Dedup: removed {dupes} duplicate netids (kept latest lasttime)")

    return clean


def annotate_records(records: dict) -> dict:
    """
    Stamp every record with its published confidence tier.

    Adds three fields to each record (see validation.annotate_record):
        confidence     "ssid_correlated" | "oui_high" | "oui_mfr"
        oui_tier       "high" | "mfr"           (from data/flock_ouis.csv)
        out_of_market  bool                     (outside PRIMARY_MARKET_COUNTRIES)
        blocked_reason which denylist rule identified the record as other hardware
                       (empty unless confidence == "identified_other")

    Runs over the *merged* record set immediately before writing, so records
    carried over from a previous scan — which were loaded back without these
    fields — are annotated too, and a tier change in the CSV is re-applied to
    old records on the next scan instead of only affecting new ones.
    """
    try:
        tiers = oui_tier_map()
    except Exception as exc:              # missing CSV → every OUI is 'high'
        print(f"  [!] Could not read OUI tiers ({exc}) — defaulting to 'high'")
        tiers = {}
    for net in records.values():
        annotate_record(net, tiers)
    return records


def write_geojson(records: dict, output_path: Path) -> None:
    """
    Write networks as GeoJSON FeatureCollection for the web map.

    Coordinates are written at FULL precision (never modified) so the map is
    accurate. Every feature is tagged match_confidence="suspected" — an OUI
    match is a heuristic, not a confirmation — plus three evidence fields that
    let consumers tell the populations apart (see validation.annotate_record):
        confidence     ssid_correlated | oui_high | oui_mfr
        oui_tier       high | mfr
        out_of_market  true when the record is outside the primary Flock market

    The top level carries the matching summary (total / actionable / counts per
    tier) so a consumer can show an honest headline without re-deriving it.
    """
    features = []
    for netid, net in sorted(records.items()):
        lat = net.get("trilat")
        lon = net.get("trilong")
        if not is_valid_latlon(lat, lon):
            continue
        lat = float(lat)
        lon = float(lon)


        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
            "properties": {
                "netid": net.get("netid", netid),
                "ssid": net.get("ssid", ""),
                "oui": net.get("oui_match", netid[:8]),
                "match_confidence": MATCH_CONFIDENCE,
                "confidence": net.get("confidence", ""),
                "oui_tier": net.get("oui_tier", ""),
                "out_of_market": bool(net.get("out_of_market", False)),
                "blocked_reason": net.get("blocked_reason", ""),
                "channel": net.get("channel"),
                "encryption": net.get("encryption", ""),
                "firsttime": net.get("firsttime", ""),
                "lasttime": net.get("lasttime", ""),
                "city": net.get("city", ""),
                "region": net.get("region", ""),
                "country": net.get("country", ""),
                "road": net.get("road", ""),
                "postalcode": net.get("postalcode", ""),
            },
        }
        features.append(feature)

    summary = summarize_confidence(records.values())

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "release_schema_version": 2,
            "release_status": "complete",
            "suppression_applied": True,
            "generated": datetime.now(timezone.utc).isoformat(),
            "source": "WiGLE (wigle.net)",
            "project": "flock-finder",
            "description": "SUSPECTED Flock Safety ALPR camera locations via WiFi "
                           "OUI matching. An OUI match is not a confirmation.",
            "match_confidence": MATCH_CONFIDENCE,
            "oui_research": "@NitekryDPaul",

            # Suppression removes identified unrelated hardware before writing,
            # so every feature in this release is actionable under this policy.
            "total_cameras": len(features),
            "total_features": len(features),
            "total_actionable": summary["actionable"],
            "ssid_correlated": summary["by_confidence"].get("ssid_correlated", 0),
            "oui_only": (
                summary["by_confidence"].get("oui_high", 0)
                + summary["by_confidence"].get("oui_mfr", 0)
            ),
            "by_confidence": summary["by_confidence"],
            "by_oui_tier": summary["by_oui_tier"],
            "out_of_market": summary["out_of_market"],
            "confidence_tiers": {
                "ssid_correlated": "SSID matches an anchored Flock naming pattern",
                "oui_high": "High-confidence Flock OUI, no contradicting SSID",
                "oui_mfr": "Contract-manufacturer OUI (Liteon/USI) — weakest",
                "identified_other": "SSID identifies other hardware — excluded",
            },
        },
    }

    atomic_write_json(output_path, geojson)

    print(f"  [✓] GeoJSON: {output_path}  ({len(features)} features)")



def write_csv(records: dict, output_path: Path) -> None:
    """Write networks as CSV for data analysis."""
    networks = sorted(records.values(), key=lambda n: n.get("netid", ""))

    fieldnames = [
        "netid", "ssid", "trilat", "trilong", "oui_match",
        "channel", "encryption", "firsttime", "lasttime",
        "city", "region", "country", "road", "postalcode",
        # Evidence fields — see validation.annotate_record(). Written last so any
        # positional consumer of the historical columns keeps working.
        "confidence", "oui_tier", "out_of_market", "blocked_reason",
    ]

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for net in networks:
        writer.writerow(net)
    atomic_write_text(output_path, buffer.getvalue())

    print(f"  [✓] CSV: {output_path}  ({len(networks)} rows)")


def write_geojson_per_oui(records: dict, output_dir: Path) -> None:
    """
    Write one GeoJSON file per OUI prefix into <output_dir>/by_oui/.

    Filename pattern: <output_dir>/by_oui/<OUI_SLUG>.geojson
    where OUI_SLUG is the OUI with colons replaced by underscores,
    e.g.  74:4C:A1  →  74_4C_A1.geojson
    """
    by_oui: dict[str, list] = {}
    for netid, net in records.items():
        oui = net.get("oui_match", "").upper()
        if not oui:
            oui = netid[:8].upper()
        by_oui.setdefault(oui, []).append((netid, net))

    out_dir = output_dir / "by_oui"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()

    written = 0
    existing_ouis = {
        path.stem.replace("_", ":").upper()
        for path in out_dir.glob("*.geojson")
        if is_valid_oui(path.stem.replace("_", ":"))
    }
    all_ouis = set(by_oui) | set(load_ouis()) | existing_ouis
    for oui in sorted(all_ouis):
        items = by_oui.get(oui, [])
        slug = oui.replace(":", "_")
        features = []
        subset_summary = summarize_confidence(net for _, net in items)
        for netid, net in sorted(items):
            lat = net.get("trilat")
            lon = net.get("trilong")
            if lat is None or lon is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "netid": net.get("netid", netid),
                    "ssid": net.get("ssid", ""),
                    "oui": oui,
                    "match_confidence": MATCH_CONFIDENCE,
                    "confidence": net.get("confidence", ""),
                    "oui_tier": net.get("oui_tier", ""),
                    "out_of_market": bool(net.get("out_of_market", False)),
                    "blocked_reason": net.get("blocked_reason", ""),
                    "channel": net.get("channel"),
                    "encryption": net.get("encryption", ""),
                    "firsttime": net.get("firsttime", ""),
                    "lasttime": net.get("lasttime", ""),
                    "city": net.get("city", ""),
                    "region": net.get("region", ""),
                    "country": net.get("country", ""),
                    "road": net.get("road", ""),
                    "postalcode": net.get("postalcode", ""),
                },
            })

        geojson = {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "release_schema_version": 2,
                "release_status": "complete",
                "suppression_applied": True,
                "generated": generated,
                "oui": oui,
                "source": "WiGLE (wigle.net)",
                "project": "flock-finder",
                "total_cameras": len(features),
                "total_actionable": subset_summary["actionable"],
                "by_confidence": subset_summary["by_confidence"],
            },
        }
        path = out_dir / f"{slug}.geojson"
        atomic_write_json(path, geojson)
        written += 1

    print(f"  [✓] Per-OUI GeoJSON: {out_dir}  ({written} files)")


def write_csv_per_oui(records: dict, output_dir: Path) -> None:
    """
    Write one CSV file per OUI prefix into <output_dir>/by_oui/.

    Filename pattern: <output_dir>/by_oui/<OUI_SLUG>.csv
    """
    by_oui: dict[str, list] = {}
    for netid, net in records.items():
        oui = net.get("oui_match", "").upper()
        if not oui:
            oui = netid[:8].upper()
        by_oui.setdefault(oui, []).append(net)

    out_dir = output_dir / "by_oui"
    out_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "netid", "ssid", "trilat", "trilong", "oui_match",
        "channel", "encryption", "firsttime", "lasttime",
        "city", "region", "country", "road", "postalcode",
        "confidence", "oui_tier", "out_of_market", "blocked_reason",
    ]

    written = 0
    existing_ouis = {
        path.stem.replace("_", ":").upper()
        for path in out_dir.glob("*.csv")
        if is_valid_oui(path.stem.replace("_", ":"))
    }
    all_ouis = set(by_oui) | set(load_ouis()) | existing_ouis
    for oui in sorted(all_ouis):
        nets = by_oui.get(oui, [])
        slug = oui.replace(":", "_")
        path = out_dir / f"{slug}.csv"
        nets_sorted = sorted(nets, key=lambda n: n.get("netid", ""))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for net in nets_sorted:
            writer.writerow(net)
        atomic_write_text(path, buffer.getvalue())
        written += 1

    print(f"  [✓] Per-OUI CSV:     {out_dir}  ({written} files)")


def write_stats(records: dict, ouis_queried: int, api_requests: int,
                new_this_scan: int, output_path: Path,
                suppression_summary: dict = None,
                authoritative_rebuild: bool = False,
                classifier_validation: dict = None) -> None:
    """Write scan statistics JSON."""
    now = datetime.now(timezone.utc)
    networks = list(records.values())

    # Count by OUI
    oui_counts = {}
    for net in networks:
        oui = net.get("oui_match", "unknown")
        oui_counts[oui] = oui_counts.get(oui, 0) + 1

    # Count by state/region
    region_counts = {}
    for net in networks:
        region = net.get("region", "unknown")
        if region:
            region_counts[region] = region_counts.get(region, 0) + 1

    # Count by country
    country_counts = {}
    for net in networks:
        country = net.get("country", "unknown")
        if country:
            country_counts[country] = country_counts.get(country, 0) + 1

    # Evidence-tier breakdown — see validation.summarize_confidence().
    summary = summarize_confidence(networks)
    suppression_summary = suppression_summary or {
        "excluded_from_release": 0,
        "new_registry_entries": 0,
        "by_reason": {},
        "registry_entries": 0,
        "registry_by_reason": {},
    }

    stats = {
        "release_schema_version": 2,
        "release_status": "complete",
        "suppression_applied": True,
        "scan_timestamp": now.isoformat(),
        # Suppression removes identified unrelated hardware before this summary.
        "total_cameras": len(networks),
        "total_actionable": summary["actionable"],
        "new_this_scan": new_this_scan,
        "unique_ouis_found": len(oui_counts),
        "ouis_queried": ouis_queried,
        "api_requests": api_requests,
        "data_retention_days": MAX_AGE_DAYS,
        # Evidence-tier breakdown (validation.summarize_confidence). Published so
        # the headline can distinguish SSID-correlated / OUI-only evidence
        # records instead of presenting every OUI hit as an equal "camera".
        "confidence_summary": summary,
        "by_confidence": summary["by_confidence"],
        "by_oui_tier": summary["by_oui_tier"],
        "out_of_market": summary["out_of_market"],
        "ssid_correlated": summary["by_confidence"].get("ssid_correlated", 0),
        "oui_only": (
            summary["by_confidence"].get("oui_high", 0)
            + summary["by_confidence"].get("oui_mfr", 0)
        ),
        "suppression": suppression_summary,
        "classifier_validation": classifier_validation or {},
        "cameras_by_oui": dict(sorted(oui_counts.items(), key=lambda x: -x[1])),
        "cameras_by_region": dict(sorted(region_counts.items(), key=lambda x: -x[1])[:50]),
        "cameras_by_country": dict(sorted(country_counts.items(), key=lambda x: -x[1])),
    }
    previous_rebuild = None
    if output_path.exists():
        try:
            previous_rebuild = json.loads(
                output_path.read_text(encoding="utf-8")
            ).get("last_authoritative_rebuild")
        except (OSError, ValueError, AttributeError):
            previous_rebuild = None
    if authoritative_rebuild:
        stats["last_authoritative_rebuild"] = now.isoformat()
    elif previous_rebuild:
        stats["last_authoritative_rebuild"] = previous_rebuild

    atomic_write_json(output_path, stats)

    print(f"  [✓] Stats: {output_path}")





# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query WiGLE for Flock Safety ALPR cameras by WiFi OUI"
    )
    parser.add_argument(
        "--oui", type=str, default=None,
        help="Query a single OUI prefix (e.g., '70:C9:4E'). Default: every OUI "
             "in data/flock_ouis.csv."
    )
    parser.add_argument(
        "--country", type=str, default=None,
        help="Filter by ISO country code (e.g., 'US'). Default: worldwide."
    )
    parser.add_argument(
        "--bbox", type=str, default=None,
        help="Bounding box as lat1,lon1,lat2,lon2 (e.g., '37,-97,39,-94')"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Output directory (default: data/)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Verify auth and print OUI list without querying"
    )
    parser.add_argument(
        "--no-merge", action="store_true",
        help="Don't merge with existing data — start fresh"
    )
    parser.add_argument(
        "--full-rescan", action="store_true",
        help="Ignore saved scan state — query the full retention window for every OUI"
    )
    parser.add_argument(
        "--authoritative-rebuild", action="store_true",
        help="Build a full replacement in resumable staging and publish only after every OUI completes"
    )
    parser.add_argument(
        "--rebuild-if-due", action="store_true",
        help="Use authoritative mode when due, otherwise run the normal incremental scan"
    )
    parser.add_argument(
        "--max-age-days", type=int, default=MAX_AGE_DAYS,
        help=f"Prune records older than this many days (default: {MAX_AGE_DAYS})"
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir) if args.output_dir else DATA_DIR
    policy = load_rebuild_policy()
    if args.rebuild_if_due:
        stats_path = output_dir / "scan_stats.json"
        try:
            current_stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
        except ValueError:
            current_stats = {}
        if not isinstance(current_stats, dict):
            current_stats = {}
        args.authoritative_rebuild = select_authoritative_rebuild(
            args.authoritative_rebuild,
            args.rebuild_if_due,
            current_stats,
            policy,
        )
        if args.authoritative_rebuild:
            print("Authoritative rebuild is due. Selecting authoritative mode.")
        else:
            print("Authoritative rebuild is not due. Continuing in incremental mode.")
    if args.authoritative_rebuild:
        args.no_merge = True
        if args.oui or args.country or args.bbox:
            print("Authoritative rebuilds require the full OUI list and unscoped geography.")
            return 1

    # ── Banner ────────────────────────────────────────────────────────────────
    banner = "=" * 62
    print(banner)
    print("  Flock Finder — WiGLE ALPR Camera Mapper")
    print("  OUI research: @NitekryDPaul  |  Inspired by DeFlock")
    print("  Mode: CUMULATIVE (merge + incremental + 2-year retention)")
    print(banner)

    # ── Load credentials ──────────────────────────────────────────────────────
    print("\n[1/7] Loading credentials…")
    load_dotenv(ENV_FILE)

    api_name = os.getenv("WIGLE_API_NAME")
    api_token = os.getenv("WIGLE_API_TOKEN")

    if not api_name or not api_token:
        print("  [✗] WiGLE API credentials not found.")
        print(f"      Create {ENV_FILE} with:")
        print("        WIGLE_API_NAME=your_api_name")
        print("        WIGLE_API_TOKEN=your_api_token")
        print("      Get credentials at: https://wigle.net/account")
        sys.exit(1)

    print(f"  API Name: {api_name[:12]}…")

    # ── Authenticate ──────────────────────────────────────────────────────────
    print("\n[2/7] Authenticating with WiGLE…")
    client = WiGLEClient(api_name, api_token)

    if not client.verify_auth():
        sys.exit(1)

    # ── Load OUI list ─────────────────────────────────────────────────────────
    print("\n[3/7] Loading Flock Safety OUI list…")

    if args.oui:
        ouis = [args.oui.upper()]
        print(f"  Single OUI mode: {ouis[0]}")
    else:
        try:
            ouis = load_ouis()
            print(f"  [✓] Loaded {len(ouis)} OUI prefixes from flock_ouis.csv")
        except (FileNotFoundError, ValueError) as exc:
            print(f"  [✗] Could not load complete OUI list: {exc}")
            return 1

    bbox = None
    if args.bbox:
        parts = [float(x) for x in args.bbox.split(",")]
        if len(parts) == 4:
            bbox = tuple(parts)
            print(f"  Bounding box: {bbox}")

    if args.country:
        print(f"  Country filter: {args.country}")

    # ── Load scan state for incremental mode ──────────────────────────────────
    state_name = "authoritative_rebuild_state.json" if args.authoritative_rebuild else "scan_state.json"
    state_path = output_dir / state_name

    print("\n[4/7] Loading scan state for incremental mode…")
    if args.full_rescan and not args.authoritative_rebuild:
        oui_states = {}
        print("  --full-rescan: ignoring saved state, querying full window")
    else:
        try:
            oui_states = load_scan_state(state_path)
        except ValueError as exc:
            print(f"  [✗] {exc}")
            return 1
        if oui_states:
            completed = sum(1 for s in oui_states.values() if s.get("status") == "completed")
            interrupted = sum(1 for s in oui_states.values() if s.get("status") == "interrupted")
            pending = sum(1 for s in oui_states.values() if s.get("status") == "pending_publish")
            print(f"  [✓] Loaded state: {completed} completed, {interrupted} interrupted, "
                  f"{pending} pending publication")
        else:
            print("  No prior state — first run, will do full scan for all OUIs")

    if (
        args.authoritative_rebuild
        and oui_states
        and all(state.get("status") == "completed" for state in oui_states.values())
    ):
        oui_states = {}
        print("  Starting a new authoritative campaign from the full retention window")

    # Sort OUIs: interrupted first, then never-scanned, then oldest completed
    ouis = sort_ouis_by_priority(ouis, oui_states)

    if args.dry_run:
        print("\n  [DRY RUN] Would query these OUIs (in priority order):")
        for oui in ouis:
            state = oui_states.get(oui.upper(), {})
            status = state.get("status", "never scanned")
            since = compute_since_date(state, args.max_age_days)
            label = ""
            if status == "interrupted":
                label = f"  ← RESUME from page {state.get('page', '?')}"
            elif status == "completed":
                label = f"  (incremental since {since})"
            else:
                label = f"  (full scan since {since})"
            print(f"    • {oui}  [{status}]{label}")
        print(f"\n  Total: {len(ouis)} OUI prefixes")
        sys.exit(0)

    # ── Load existing data for cumulative merge ───────────────────────────────
    geojson_out = output_dir / "flock_cameras.geojson"
    csv_out = output_dir / "flock_cameras.csv"
    stats_out = output_dir / "scan_stats.json"
    registry_path = output_dir / "suppression_registry.json"
    try:
        suppression_registry = load_suppression_registry(registry_path)
    except ValueError as exc:
        print(f"  [✗] {exc}")
        return 1

    print("\n[5/7] Loading existing data for cumulative merge…")
    if args.no_merge:
        existing = {}
        print("  --no-merge: starting with empty dataset")
        try:
            baseline_source = load_existing_data(geojson_out) if args.authoritative_rebuild else {}
        except ValueError as exc:
            print(f"  [✗] {exc}")
            return 1
    else:
        try:
            existing = load_existing_data(geojson_out)
        except ValueError as exc:
            print(f"  [✗] {exc}")
            return 1
        baseline_source = existing
    baseline_public, _baseline_registry, _baseline_suppression = suppress_records(
        baseline_source,
        suppression_registry,
        datetime.now(timezone.utc).isoformat(),
    )
    baseline_count = len(baseline_public)

    # ── Query WiGLE (incremental + resumable) ─────────────────────────────────
    print(f"\n[6/7] Querying WiGLE for {len(ouis)} OUI prefixes (incremental)…")
    print(f"  Rate limit: {RATE_LIMIT_DELAY}s between requests")
    print()

    all_new_networks = []
    pagination_outcomes = {}
    ouis_with_results = 0
    ouis_skipped_rate_limit = 0
    hit_rate_limit = False

    for i, oui in enumerate(ouis, 1):
        oui_key = oui.upper()

        if hit_rate_limit:
            ouis_skipped_rate_limit += 1
            print(f"  [{i:2d}/{len(ouis)}] Skipping OUI {oui} — rate limited")
            pagination_outcomes[oui_key] = PaginationStatus.INTERRUPTED
            continue

        # Determine incremental since_date for this OUI
        oui_state = oui_states.get(oui_key, {})
        since_date = compute_since_date(oui_state, args.max_age_days)

        if oui_state.get("status") == "pending_publish":
            pending_records = oui_state.get("pending_records", [])
            all_new_networks.extend(pending_records)
            pagination_outcomes[oui_key] = PaginationStatus.COMPLETE
            if pending_records:
                ouis_with_results += 1
            print(f"  [{i:2d}/{len(ouis)}] OUI {oui}: pagination complete, awaiting publication")
            continue

        # Check if we should resume pagination
        resume_cursor = None
        resume_page = 0
        status_label = "full"

        if oui_state.get("status") == "interrupted":
            resume_cursor = oui_state.get("search_after")
            resume_page = oui_state.get("page", 0)
            prev_fetched = len(oui_state.get("pending_records", []))
            status_label = f"RESUME from page {resume_page} ({prev_fetched} prior)"
            print(f"  [{i:2d}/{len(ouis)}] Resuming OUI {oui}  [{status_label}]  "
                  f"(lastupdt≥{since_date})")
        elif oui_state.get("status") == "completed":
            status_label = "incremental"
            print(f"  [{i:2d}/{len(ouis)}] Querying OUI {oui}  [{status_label}]  "
                  f"(lastupdt≥{since_date})")
        else:
            status_label = "first scan"
            print(f"  [{i:2d}/{len(ouis)}] Querying OUI {oui}  [{status_label}]  "
                  f"(lastupdt≥{since_date})")

        result = query_oui(
            client, oui,
            country=args.country,
            bbox=bbox,
            since_date=since_date,
            resume_search_after=resume_cursor,
            resume_page=resume_page,
        )
        prior_records = (
            oui_state.get("pending_records", [])
            if oui_state.get("status") == "interrupted" else []
        )
        networks = prior_records + result.records

        if networks:
            ouis_with_results += 1
            print(f"         → {len(networks)} cameras found")
        else:
            print("         → no new results")

        # Update per-OUI state
        now_iso = datetime.now(timezone.utc).isoformat()

        state_changed = False
        if result.status == PaginationStatus.INTERRUPTED:
            # This is resume state, not a completed scan checkpoint.
            oui_states[oui_key] = {
                "status": "interrupted",
                "search_after": result.search_after,
                "page": result.page,
                "pending_records": networks,
                "since_date": since_date,
                "interrupted_at": now_iso,
            }
            pagination_outcomes[oui_key] = result.status
            state_changed = True
            hit_rate_limit = True
            print("  ⚠ Rate limit hit — state saved, will resume next run")
        elif result.status == PaginationStatus.COMPLETE:
            # Hold results until every requested OUI is complete. The durable
            # last_completed checkpoint advances only after publication.
            oui_states[oui_key] = {
                "status": "pending_publish",
                "search_after": None,
                "page": result.page,
                "since_date": since_date,
                "pending_records": networks,
                "pagination_completed_at": now_iso,
            }
            pagination_outcomes[oui_key] = result.status
            all_new_networks.extend(networks)
            state_changed = True
        else:
            pagination_outcomes[oui_key] = result.status
            print(f"  [✗] OUI {oui} failed after retries: {result.error}")

        if state_changed:
            save_scan_state(oui_states, state_path)

        # Rate limit between OUI queries
        if i < len(ouis) and not hit_rate_limit:
            time.sleep(RATE_LIMIT_DELAY)

    # ── Merge + Prune ─────────────────────────────────────────────────────────
    print("\n[7/7] Merging and writing output…")
    print(f"  New results this scan: {len(all_new_networks)}")

    incomplete = {
        oui: getattr(pagination_outcomes.get(oui), "value", pagination_outcomes.get(oui))
        for oui in ouis
        if pagination_outcomes.get(oui) != PaginationStatus.COMPLETE
    }
    if incomplete:
        print("  [✗] Publication withheld because the scan campaign is incomplete.")
        for oui, status in incomplete.items():
            print(f"      {oui}: {status or 'missing'}")
        return 2

    merged = merge_networks(existing, all_new_networks, suppression_registry)

    # Prune old records
    merged = prune_old_records(merged, max_age_days=args.max_age_days)

    # Final dedup — guarantee no duplicate netids (safety net)
    merged = dedup_by_netid(merged)

    # ── Output validation ─────────────────────────────────────────────────────
    # Stamp the published evidence fields (confidence / oui_tier / out_of_market)
    # on every record — including records carried over from earlier scans, which
    # are loaded back without them — so the outputs and their summaries can never
    # disagree with each other or with data/flock_ouis.csv.
    annotate_records(merged)

    observed_at = datetime.now(timezone.utc).isoformat()
    merged, updated_registry, suppression_summary = suppress_records(
        merged, suppression_registry, observed_at
    )
    new_this_scan = len(merged) - baseline_count

    release_check = validate_release_candidate(
        merged,
        ouis,
        pagination_outcomes,
        baseline_count=baseline_count or None,
        minimum_record_ratio=policy["minimum_release_record_ratio"],
    )
    if not release_check.valid:
        print("  [✗] Candidate release failed closed validation.")
        for error in release_check.errors:
            print(f"      {error}")
        return 3

    try:
        classifier_validation = json.loads(
            CLASSIFIER_REPORT_FILE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(f"  [✗] Classifier validation report is unavailable: {exc}")
        return 3
    if (
        not isinstance(classifier_validation, dict)
        or classifier_validation.get("classifier") != "ssid_correlated"
        or not isinstance(classifier_validation.get("per_prefix"), dict)
    ):
        print("  [✗] Classifier validation report is malformed")
        return 3

    # Write combined outputs

    atomic_write_json(registry_path, updated_registry)
    write_geojson(merged, geojson_out)
    write_csv(merged, csv_out)
    write_stats(
        merged,
        len(ouis),
        client.request_count,
        new_this_scan,
        stats_out,
        suppression_summary=suppression_summary,
        authoritative_rebuild=args.authoritative_rebuild,
        classifier_validation=classifier_validation,
    )

    # Write per-OUI split files (data/by_oui/<OUI_SLUG>.{geojson,csv})
    write_geojson_per_oui(merged, output_dir)
    write_csv_per_oui(merged, output_dir)
    completed_at = datetime.now(timezone.utc).isoformat()
    for oui in ouis:
        state = oui_states[oui]
        oui_states[oui] = {
            "status": "completed",
            "last_completed": completed_at,
            "search_after": None,
            "page": state.get("page", 0),
            "since_date": state.get("since_date", ""),
            "results_this_scan": len(state.get("pending_records", [])),
        }
    save_scan_state(oui_states, state_path)

    # Regenerate centralized OUI metadata JSON for the frontend so the map's
    # OUI list is never hand-duplicated / out of sync with the canonical CSV.
    try:
        oui_json_path = write_oui_json(json_path=output_dir / "flock_ouis.json")
        print(f"  [✓] OUI metadata: {oui_json_path}")
    except Exception as exc:
        print(f"  [!] Could not write OUI metadata JSON: {exc}")

    # NOTE: the README / website headline is rendered from the published CSV by
    # scripts/update_confidence_stats.py, which runs after this script in
    # .github/workflows/update-data.yml. It used to be rendered right here from
    # scan_stats.json, which duplicated the counting logic and allowed the
    # headline to drift away from the dataset it described.


    # ── Summary ───────────────────────────────────────────────────────────────
    completed = sum(1 for s in oui_states.values() if s.get("status") == "completed")
    interrupted = sum(1 for s in oui_states.values() if s.get("status") == "interrupted")

    print()
    print(banner)
    print("  Scan Complete!")
    print(banner)
    print(f"  Total cameras (cumulative) : {len(merged)}")
    print(f"  New this scan              : {new_this_scan}")
    print(f"  OUIs with results          : {ouis_with_results}/{len(ouis)}")
    print(f"  OUIs completed (all-time)  : {completed}/{len(ouis)}")
    if interrupted:
        print(f"  OUIs interrupted           : {interrupted} (will resume next run)")
    if ouis_skipped_rate_limit:
        print(f"  OUIs skipped (rate limit)  : {ouis_skipped_rate_limit}")
    print(f"  API requests made          : {client.request_count}")
    if hit_rate_limit:
        print("  ⚠ Rate limited             : yes (state saved — re-run to continue)")
    print(f"  Data retention             : {args.max_age_days} days")
    print(f"  Scan state                 : {state_path}")
    print(f"  GeoJSON                    : {geojson_out}")
    print(f"  CSV                        : {csv_out}")
    print(f"  Stats                      : {stats_out}")
    print(banner)
    print()
    print("  Open docs/index.html to view the interactive map.")
    print("  Or: python3 -m http.server 8080")
    print()
    print("  💡 Next run will be incremental — only fetching updates.")
    print("     Use --full-rescan to force a complete re-query.")
    if hit_rate_limit:
        print("     Interrupted OUIs will auto-resume from where they stopped.")


if __name__ == "__main__":
    sys.exit(main())
