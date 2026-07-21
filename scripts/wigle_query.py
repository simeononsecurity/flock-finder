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
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from oui_metadata import write_oui_json

# Shared, unit-tested helpers (validation + public-data policy)
from validation import (
    MATCH_CONFIDENCE,
    PUBLIC_COORD_PRECISION,
    is_valid_oui,
    normalize_oui,
    redact_coordinates,
    validate_record,
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
    atomic_write_text(path, json.dumps(obj, indent=2))



# ─── Flock Safety OUI List ────────────────────────────────────────────────────

def load_ouis(oui_file: Path = None) -> list:
    """
    Load and validate Flock Safety OUI prefixes from the canonical CSV.

    Malformed rows are skipped with a warning, and duplicates are removed,
    so a bad edit to flock_ouis.csv can never inject a garbage query.
    """
    if oui_file is None:
        oui_file = DATA_DIR / "flock_ouis.csv"

    ouis = []
    seen = set()
    with open(oui_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("oui") or "").strip()
            if not is_valid_oui(raw):
                if raw:
                    print(f"  [!] Skipping malformed OUI in CSV: {raw!r}")
                continue
            oui = normalize_oui(raw)
            if oui in seen:
                continue
            seen.add(oui)
            ouis.append(oui)
    return ouis



# All 31 known Flock Safety OUI prefixes (fallback if CSV missing)
FLOCK_OUIS_FALLBACK = [
    "70:C9:4E", "3C:91:80", "D8:F3:BC", "80:30:49", "B8:35:32",
    "14:5A:FC", "74:4C:A1", "08:3A:88", "9C:2F:9D", "C0:35:32",
    "94:08:53", "E4:AA:EA", "F4:6A:DD", "F8:A2:D6", "24:B2:B9",
    "00:F4:8D", "D0:39:57", "E8:D0:FC", "E0:4F:43", "B8:1E:A4",
    "70:08:94", "58:8E:81", "EC:1B:BD", "3C:71:BF", "58:00:E3",
    "90:35:EA", "5C:93:A2", "64:6E:69", "48:27:EA", "A4:CF:12",
    "82:6B:F2",
]


# ─── Scan State Persistence ──────────────────────────────────────────────────

def load_scan_state(state_path: Path = None) -> dict:
    """
    Load per-OUI scan state from disk.

    Returns a dict keyed by OUI prefix (uppercase), e.g.:
        {
            "70:C9:4E": {
                "last_completed": "2025-07-14T12:00:00+00:00",
                "status": "completed",       # completed | interrupted
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
        return data.get("ouis", {})
    except Exception as exc:
        print(f"  [!] Could not load scan state: {exc}")
        return {}


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

    if status == "interrupted":
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
        elif status == "never" or not status:
            return (1, "")  # Second priority — never scanned
        else:
            # Completed — sort oldest first so stale data gets refreshed
            return (2, state.get("last_completed", ""))

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
                return {"success": False, "error": "rate_limited"}
            elif resp.status_code == 404:
                # WiGLE returns 404 when no results match the search
                return {"success": True, "results": [], "totalResults": 0}
            else:
                print(f"  [!] HTTP {resp.status_code}: {resp.text[:200]}")
                return {"success": False, "error": f"http_{resp.status_code}"}
        except requests.Timeout:
            print("  [!] Request timed out")
            return {"success": False, "error": "timeout"}
        except Exception as exc:
            print(f"  [!] Request error: {exc}")
            return {"success": False, "error": str(exc)}


# ─── Query Logic ──────────────────────────────────────────────────────────────

def query_oui(client: WiGLEClient, oui: str, country: str = None,
              bbox: tuple = None, since_date: str = None,
              resume_search_after: str = None,
              resume_page: int = 0) -> tuple:
    """
    Query WiGLE for ALL WiFi networks matching a given OUI prefix.
    Paginates through every page until no more results.

    Args:
        since_date: YYYYMMDD — only fetch records updated after this date
        resume_search_after: Pagination cursor to resume from (if resuming)
        resume_page: Page number to resume from (cosmetic, for logging)

    Returns:
        (networks, rate_limited, final_search_after, final_page)
        - networks: list of network dicts
        - rate_limited: bool — whether we hit the API rate limit
        - final_search_after: last pagination cursor (for save/resume)
        - final_page: last page number reached
    """
    networks = []
    search_after = resume_search_after
    page = resume_page
    rate_limited = False

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

        result = client.search_wifi(**kwargs)

        if not result.get("success", False):
            error = result.get("error", "unknown")
            if error == "rate_limited":
                print(f"    ⚠ Rate limited after {len(networks)} results — saving state for resume")
                rate_limited = True
                break
            elif error == "unauthorized":
                break
            # Retry once on transient errors
            time.sleep(RATE_LIMIT_DELAY * 3)
            result = client.search_wifi(**kwargs)
            if not result.get("success", False):
                break

        results = result.get("results", [])
        if not results:
            break

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
        if not search_after or len(results) < PAGE_SIZE:
            break  # No more pages

        # Rate limiting between pages
        time.sleep(RATE_LIMIT_DELAY)

    return networks, rate_limited, search_after, page


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
        print(f"  [!] Could not load existing data: {exc}")

    return existing


def merge_networks(existing: dict, new_networks: list) -> dict:
    """
    Merge new scan results into existing data.
    For duplicate netids, keep the record with the most recent lasttime.
    """
    merged = dict(existing)  # Start with existing data

    updated = 0
    added = 0

    for net in new_networks:
        netid = net["netid"].upper()
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


def write_geojson(records: dict, output_path: Path) -> None:
    """
    Write networks as GeoJSON FeatureCollection for the web map.

    Data policy: coordinates are truncated to PUBLIC_COORD_PRECISION and every
    feature is explicitly tagged match_confidence="suspected" — an OUI match
    is a heuristic, not a confirmation.
    """
    features = []
    for netid, net in sorted(records.items()):
        lat, lon = redact_coordinates(net.get("trilat"), net.get("trilong"))
        if lat is None or lon is None:
            continue

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

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "source": "WiGLE (wigle.net)",
            "project": "flock-finder",
            "description": "SUSPECTED Flock Safety ALPR camera locations via WiFi "
                           "OUI matching. An OUI match is not a confirmation.",
            "match_confidence": MATCH_CONFIDENCE,
            "coordinate_precision_decimals": PUBLIC_COORD_PRECISION,
            "oui_research": "@NitekryDPaul",
            "total_cameras": len(features),
        },
    }

    atomic_write_json(output_path, geojson)

    print(f"  [✓] GeoJSON: {output_path}  ({len(features)} features)")



def write_csv(records: dict, output_path: Path) -> None:
    """Write networks as CSV for data analysis."""
    networks = sorted(records.values(), key=lambda n: n.get("netid", ""))
    if not networks:
        print("  [!] No networks to write to CSV")
        return

    fieldnames = [
        "netid", "ssid", "trilat", "trilong", "oui_match",
        "channel", "encryption", "firsttime", "lasttime",
        "city", "region", "country", "road", "postalcode",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for net in networks:
            writer.writerow(net)

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
    for oui, items in sorted(by_oui.items()):
        slug = oui.replace(":", "_")
        features = []
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
                "generated": generated,
                "oui": oui,
                "source": "WiGLE (wigle.net)",
                "project": "flock-finder",
                "total_cameras": len(features),
            },
        }
        path = out_dir / f"{slug}.geojson"
        with open(path, "w") as f:
            json.dump(geojson, f, indent=2)
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
    ]

    written = 0
    for oui, nets in sorted(by_oui.items()):
        slug = oui.replace(":", "_")
        path = out_dir / f"{slug}.csv"
        nets_sorted = sorted(nets, key=lambda n: n.get("netid", ""))
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for net in nets_sorted:
                writer.writerow(net)
        written += 1

    print(f"  [✓] Per-OUI CSV:     {out_dir}  ({written} files)")


def write_stats(records: dict, ouis_queried: int, api_requests: int,
                new_this_scan: int, output_path: Path) -> None:
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

    stats = {
        "scan_timestamp": now.isoformat(),
        "total_cameras": len(networks),
        "new_this_scan": new_this_scan,
        "unique_ouis_found": len(oui_counts),
        "ouis_queried": ouis_queried,
        "api_requests": api_requests,
        "data_retention_days": MAX_AGE_DAYS,
        "cameras_by_oui": dict(sorted(oui_counts.items(), key=lambda x: -x[1])),
        "cameras_by_region": dict(sorted(region_counts.items(), key=lambda x: -x[1])[:50]),
        "cameras_by_country": dict(sorted(country_counts.items(), key=lambda x: -x[1])),
    }

    atomic_write_json(output_path, stats)

    print(f"  [✓] Stats: {output_path}")



def update_readme(stats_path: Path, readme_path: Path = None) -> None:
    """
    Auto-update the README.md stats section between STATS_START/STATS_END markers.
    Reads scan_stats.json and replaces the stats table in README.md.
    """
    if readme_path is None:
        readme_path = PROJECT_DIR / "README.md"

    if not stats_path.exists() or not readme_path.exists():
        return

    try:
        with open(stats_path, "r") as f:
            stats = json.load(f)

        total = stats.get("total_cameras", 0)
        ouis_found = stats.get("unique_ouis_found", 0)
        countries = stats.get("cameras_by_country", {})

        regions = stats.get("cameras_by_region", {})
        retention = stats.get("data_retention_days", 730)
        timestamp = stats.get("scan_timestamp", "")[:10]

        new_stats = (
            "<!-- STATS_START -->\n"
            "| Metric | Value |\n"
            "|--------|-------|\n"
            f"| 📸 **Cameras Mapped** | {total:,} |\n"
            f"| 📡 **OUI Prefixes with Data** | {ouis_found} / 31 |\n"
            f"| 🌎 **Countries** | {len(countries)} |\n"
            f"| 🗺️ **Regions** | {len(regions)} |\n"
            f"| 🕐 **Last Updated** | {timestamp} |\n"
            f"| 📦 **Data Retention** | {retention} days ({retention // 365} years) |\n"
            "<!-- STATS_END -->"
        )

        with open(readme_path, "r") as f:
            content = f.read()

        import re
        pattern = r"<!-- STATS_START -->.*?<!-- STATS_END -->"
        if re.search(pattern, content, re.DOTALL):
            updated = re.sub(pattern, new_stats, content, flags=re.DOTALL)
            with open(readme_path, "w") as f:
                f.write(updated)
            print(f"  [✓] README: updated stats ({total:,} cameras, {len(countries)} countries)")
        else:
            print("  [!] README: no STATS_START/STATS_END markers found — skipping")

    except Exception as exc:
        print(f"  [!] README update failed: {exc}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query WiGLE for Flock Safety ALPR cameras by WiFi OUI"
    )
    parser.add_argument(
        "--oui", type=str, default=None,
        help="Query a single OUI prefix (e.g., '70:C9:4E'). Default: all 31 OUIs."
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
        "--max-age-days", type=int, default=MAX_AGE_DAYS,
        help=f"Prune records older than this many days (default: {MAX_AGE_DAYS})"
    )
    args = parser.parse_args()

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
        except FileNotFoundError:
            ouis = FLOCK_OUIS_FALLBACK
            print(f"  [!] CSV not found — using {len(ouis)} built-in OUIs")

    bbox = None
    if args.bbox:
        parts = [float(x) for x in args.bbox.split(",")]
        if len(parts) == 4:
            bbox = tuple(parts)
            print(f"  Bounding box: {bbox}")

    if args.country:
        print(f"  Country filter: {args.country}")

    # ── Load scan state for incremental mode ──────────────────────────────────
    output_dir = Path(args.output_dir) if args.output_dir else DATA_DIR
    state_path = output_dir / "scan_state.json"

    print("\n[4/7] Loading scan state for incremental mode…")
    if args.full_rescan:
        oui_states = {}
        print("  --full-rescan: ignoring saved state, querying full window")
    else:
        oui_states = load_scan_state(state_path)
        if oui_states:
            completed = sum(1 for s in oui_states.values() if s.get("status") == "completed")
            interrupted = sum(1 for s in oui_states.values() if s.get("status") == "interrupted")
            print(f"  [✓] Loaded state: {completed} completed, {interrupted} interrupted, "
                  f"{len(ouis) - completed - interrupted} never scanned")
        else:
            print("  No prior state — first run, will do full scan for all OUIs")

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

    print("\n[5/7] Loading existing data for cumulative merge…")
    if args.no_merge:
        existing = {}
        print("  --no-merge: starting with empty dataset")
    else:
        existing = load_existing_data(geojson_out)

    # ── Query WiGLE (incremental + resumable) ─────────────────────────────────
    print(f"\n[6/7] Querying WiGLE for {len(ouis)} OUI prefixes (incremental)…")
    print(f"  Rate limit: {RATE_LIMIT_DELAY}s between requests")
    print()

    all_new_networks = []
    ouis_with_results = 0
    ouis_skipped_rate_limit = 0
    hit_rate_limit = False

    for i, oui in enumerate(ouis, 1):
        oui_key = oui.upper()

        if hit_rate_limit:
            ouis_skipped_rate_limit += 1
            print(f"  [{i:2d}/{len(ouis)}] Skipping OUI {oui} — rate limited")
            continue

        # Determine incremental since_date for this OUI
        oui_state = oui_states.get(oui_key, {})
        since_date = compute_since_date(oui_state, args.max_age_days)

        # Check if we should resume pagination
        resume_cursor = None
        resume_page = 0
        status_label = "full"

        if oui_state.get("status") == "interrupted":
            resume_cursor = oui_state.get("search_after")
            resume_page = oui_state.get("page", 0)
            prev_fetched = oui_state.get("fetched_so_far", 0)
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

        networks, was_limited, final_cursor, final_page = query_oui(
            client, oui,
            country=args.country,
            bbox=bbox,
            since_date=since_date,
            resume_search_after=resume_cursor,
            resume_page=resume_page,
        )

        if networks:
            ouis_with_results += 1
            all_new_networks.extend(networks)
            print(f"         → {len(networks)} cameras found")
        else:
            print("         → no new results")

        # Update per-OUI state
        now_iso = datetime.now(timezone.utc).isoformat()

        if was_limited:
            # Save interrupted state for resumption
            prev_fetched = oui_state.get("fetched_so_far", 0) if oui_state.get("status") == "interrupted" else 0
            oui_states[oui_key] = {
                "status": "interrupted",
                "search_after": final_cursor,
                "page": final_page,
                "fetched_so_far": prev_fetched + len(networks),
                "since_date": since_date,
                "interrupted_at": now_iso,
            }
            hit_rate_limit = True
            print("  ⚠ Rate limit hit — state saved, will resume next run")
        else:
            # Completed successfully
            oui_states[oui_key] = {
                "status": "completed",
                "last_completed": now_iso,
                "search_after": None,
                "page": final_page,
                "fetched_so_far": 0,
                "since_date": since_date,
                "results_this_scan": len(networks),
            }

        # Save state after EVERY OUI so we don't lose progress on crash/kill
        save_scan_state(oui_states, state_path)

        # Rate limit between OUI queries
        if i < len(ouis) and not hit_rate_limit:
            time.sleep(RATE_LIMIT_DELAY)

    # ── Merge + Prune ─────────────────────────────────────────────────────────
    print("\n[7/7] Merging and writing output…")
    print(f"  New results this scan: {len(all_new_networks)}")

    count_before = len(existing)
    merged = merge_networks(existing, all_new_networks)
    new_this_scan = len(merged) - count_before

    # Prune old records
    merged = prune_old_records(merged, max_age_days=args.max_age_days)

    # Final dedup — guarantee no duplicate netids (safety net)
    merged = dedup_by_netid(merged)

    # ── Output validation + public-data precision policy ──────────────────────
    # Do this ONCE, centrally, so every downstream writer (combined GeoJSON/CSV
    # and per-OUI splits) emits only validated records at the reduced public
    # coordinate precision. This is the single choke point for the data policy.
    public = {}
    dropped = 0
    for netid, net in merged.items():
        rec = dict(net)
        rlat, rlon = redact_coordinates(net.get("trilat"), net.get("trilong"))
        rec["trilat"] = rlat
        rec["trilong"] = rlon
        if not validate_record(rec):
            dropped += 1
            continue
        public[netid] = rec
    if dropped:
        print(f"  Output validation: dropped {dropped} invalid/incomplete record(s)")
    merged = public

    # Write combined outputs

    write_geojson(merged, geojson_out)
    write_csv(merged, csv_out)
    write_stats(merged, len(ouis), client.request_count, new_this_scan, stats_out)

    # Write per-OUI split files (data/by_oui/<OUI_SLUG>.{geojson,csv})
    write_geojson_per_oui(merged, output_dir)
    write_csv_per_oui(merged, output_dir)

    # Regenerate centralized OUI metadata JSON for the frontend so the map's
    # OUI list is never hand-duplicated / out of sync with the canonical CSV.
    try:
        oui_json_path = write_oui_json(json_path=output_dir / "flock_ouis.json")
        print(f"  [✓] OUI metadata: {oui_json_path}")
    except Exception as exc:
        print(f"  [!] Could not write OUI metadata JSON: {exc}")

    # Update README with cumulative stats
    update_readme(stats_out)


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
    main()
