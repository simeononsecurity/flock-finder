#!/usr/bin/env python3
"""
Flock Finder — WiGLE Dataset Query
===================================
Queries the WiGLE WiFi database for networks matching known Flock Safety
ALPR camera OUI prefixes. Outputs GeoJSON + CSV for the web map.

Data is CUMULATIVE — each run merges new results with existing data.
Records older than 2 years (based on lasttime) are pruned automatically.
All pages are fetched for each OUI (full pagination).

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
"""

import csv
import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

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

# Output files
GEOJSON_OUTPUT = DATA_DIR / "flock_cameras.geojson"
CSV_OUTPUT = DATA_DIR / "flock_cameras.csv"
STATS_OUTPUT = DATA_DIR / "scan_stats.json"


# ─── Flock Safety OUI List ────────────────────────────────────────────────────

def load_ouis(oui_file: Path = None) -> list:
    """Load Flock Safety OUI prefixes from CSV dataset."""
    if oui_file is None:
        oui_file = DATA_DIR / "flock_ouis.csv"

    ouis = []
    with open(oui_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            oui = row["oui"].strip().upper()
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


# ─── WiGLE API Client ────────────────────────────────────────────────────────

class WiGLEClient:
    """Thin wrapper around the WiGLE v2 REST API."""

    def __init__(self, api_name: str, api_token: str):
        self.session = requests.Session()
        self.session.auth = (api_name, api_token)
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "flock-finder/1.0 (https://github.com/dagnazty/flock-finder)",
        })
        self.request_count = 0

    def verify_auth(self) -> bool:
        """Verify API credentials are valid."""
        try:
            resp = self.session.get(f"{WIGLE_API_BASE}/profile/user", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                user = data.get("userid", "unknown")
                print(f"  [✓] Authenticated as: {user}")
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
              bbox: tuple = None, since_date: str = None) -> list:
    """
    Query WiGLE for ALL WiFi networks matching a given OUI prefix.
    Paginates through every page until no more results.

    Args:
        since_date: YYYYMMDD — only fetch records updated after this date

    Returns list of network dicts with location data.
    """
    networks = []
    search_after = None
    page = 0
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
                print(f"    ⚠ Rate limited after {len(networks)} results — stopping this OUI")
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

    return networks, rate_limited


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
    """Write networks as GeoJSON FeatureCollection for the web map."""
    features = []
    for netid, net in sorted(records.items()):
        lat = net.get("trilat")
        lon = net.get("trilong")
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
            "description": "Flock Safety ALPR camera locations via WiFi OUI matching",
            "oui_research": "@NitekryDPaul",
            "total_cameras": len(features),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(geojson, f, indent=2)

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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(stats, f, indent=2)

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
        ouis_queried = stats.get("ouis_queried", 0)
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
        "--max-age-days", type=int, default=MAX_AGE_DAYS,
        help=f"Prune records older than this many days (default: {MAX_AGE_DAYS})"
    )
    args = parser.parse_args()

    # ── Banner ────────────────────────────────────────────────────────────────
    banner = "=" * 62
    print(banner)
    print("  Flock Finder — WiGLE ALPR Camera Mapper")
    print("  OUI research: @NitekryDPaul  |  Inspired by DeFlock")
    print("  Mode: CUMULATIVE (merge + 2-year retention)")
    print(banner)

    # ── Load credentials ──────────────────────────────────────────────────────
    print("\n[1/6] Loading credentials…")
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
    print("\n[2/6] Authenticating with WiGLE…")
    client = WiGLEClient(api_name, api_token)

    if not client.verify_auth():
        sys.exit(1)

    # ── Load OUI list ─────────────────────────────────────────────────────────
    print("\n[3/6] Loading Flock Safety OUI list…")

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

    # Calculate the 2-year-ago date for the WiGLE lastupdt filter
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=args.max_age_days))
    since_date = cutoff_date.strftime("%Y%m%d")
    print(f"  Data retention: {args.max_age_days} days (since {cutoff_date.strftime('%Y-%m-%d')})")

    if args.dry_run:
        print("\n  [DRY RUN] Would query these OUIs:")
        for oui in ouis:
            print(f"    • {oui}")
        print(f"\n  Total: {len(ouis)} OUI prefixes")
        print(f"  All pages will be fetched per OUI (full pagination)")
        sys.exit(0)

    # ── Load existing data for cumulative merge ───────────────────────────────
    output_dir = Path(args.output_dir) if args.output_dir else DATA_DIR
    geojson_out = output_dir / "flock_cameras.geojson"
    csv_out = output_dir / "flock_cameras.csv"
    stats_out = output_dir / "scan_stats.json"

    print(f"\n[4/6] Loading existing data for cumulative merge…")
    if args.no_merge:
        existing = {}
        print("  --no-merge: starting with empty dataset")
    else:
        existing = load_existing_data(geojson_out)

    # ── Query WiGLE ───────────────────────────────────────────────────────────
    print(f"\n[5/6] Querying WiGLE for {len(ouis)} OUI prefixes (all pages)…")
    print(f"  Rate limit: {RATE_LIMIT_DELAY}s between requests")
    print(f"  WiGLE lastupdt filter: {since_date}")
    print()

    all_new_networks = []
    ouis_with_results = 0
    hit_rate_limit = False

    for i, oui in enumerate(ouis, 1):
        if hit_rate_limit:
            print(f"  [{i:2d}/{len(ouis)}] Skipping OUI {oui} — rate limited")
            continue

        print(f"  [{i:2d}/{len(ouis)}] Querying OUI {oui}…")

        networks, was_limited = query_oui(
            client, oui,
            country=args.country,
            bbox=bbox,
            since_date=since_date,
        )

        if networks:
            ouis_with_results += 1
            all_new_networks.extend(networks)
            print(f"         → {len(networks)} cameras found")
        else:
            print(f"         → no results")

        if was_limited:
            hit_rate_limit = True
            print("  ⚠ Rate limit hit — stopping further OUI queries")
            print("    Re-run tomorrow to continue from where we left off")

        # Rate limit between OUI queries
        if i < len(ouis) and not hit_rate_limit:
            time.sleep(RATE_LIMIT_DELAY)

    # ── Merge + Prune ─────────────────────────────────────────────────────────
    print(f"\n[6/6] Merging and writing output…")
    print(f"  New results this scan: {len(all_new_networks)}")

    count_before = len(existing)
    merged = merge_networks(existing, all_new_networks)
    new_this_scan = len(merged) - count_before

    # Prune old records
    merged = prune_old_records(merged, max_age_days=args.max_age_days)

    # Final dedup — guarantee no duplicate netids (safety net)
    merged = dedup_by_netid(merged)

    # Write outputs
    write_geojson(merged, geojson_out)
    write_csv(merged, csv_out)
    write_stats(merged, len(ouis), client.request_count, new_this_scan, stats_out)

    # Update README with cumulative stats
    update_readme(stats_out)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(banner)
    print(f"  Scan Complete!")
    print(banner)
    print(f"  Total cameras (cumulative) : {len(merged)}")
    print(f"  New this scan              : {new_this_scan}")
    print(f"  OUIs with results          : {ouis_with_results}/{len(ouis)}")
    print(f"  API requests made          : {client.request_count}")
    if hit_rate_limit:
        print(f"  ⚠ Rate limited             : yes (re-run tomorrow)")
    print(f"  Data retention             : {args.max_age_days} days")
    print(f"  GeoJSON                    : {geojson_out}")
    print(f"  CSV                        : {csv_out}")
    print(f"  Stats                      : {stats_out}")
    print(banner)
    print()
    print("  Open docs/index.html to view the interactive map.")
    print("  Or: python3 -m http.server 8080")


if __name__ == "__main__":
    main()
