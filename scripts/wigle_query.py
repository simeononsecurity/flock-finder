#!/usr/bin/env python3
"""
Flock Finder — WiGLE Dataset Query
===================================
Queries the WiGLE WiFi database for networks matching known Flock Safety
ALPR camera OUI prefixes. Outputs GeoJSON + CSV for the web map.

Based on research by @NitekryDPaul (promiscuous-mode OUI discovery) and
the DeFlock project (https://www.deflock.me).

Similar to: https://github.com/simeononsecurity/track-openroaming-passpoint

WiGLE API docs: https://api.wigle.net

Usage:
    python3 scripts/wigle_query.py                    # Full scan, all OUIs
    python3 scripts/wigle_query.py --oui 70:c9:4e     # Single OUI
    python3 scripts/wigle_query.py --bbox 37,-97,39,-94  # Bounding box (lat,lon,lat,lon)
    python3 scripts/wigle_query.py --country US        # Country filter
"""

import csv
import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone
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
MAX_RESULTS_PER_OUI = 1000  # max results per OUI (WiGLE pages at 100)
PAGE_SIZE = 100              # WiGLE default page size

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
                    results_per_page: int = PAGE_SIZE) -> dict:
        """
        Search WiGLE WiFi database.

        Args:
            netid: BSSID/MAC address pattern (e.g., "70:C9:4E" for OUI prefix)
            ssid: SSID pattern to search
            latrange1/2, longrange1/2: Bounding box coordinates
            country: ISO country code (e.g., "US")
            search_after: Pagination token from previous response
            results_per_page: Number of results per page (max 100)

        Returns:
            API response dict with 'success', 'results', 'searchAfter', etc.
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
              bbox: tuple = None, max_results: int = MAX_RESULTS_PER_OUI) -> list:
    """
    Query WiGLE for all WiFi networks matching a given OUI prefix.

    The WiGLE netid search supports partial BSSID matching — passing
    "70:C9:4E" will match all MACs starting with that prefix.

    Returns list of network dicts with location data.
    """
    networks = []
    search_after = None
    page = 0

    # Build the netid search pattern — WiGLE accepts OUI prefix with wildcard
    # Format: "70:C9:4E" matches "70:C9:4E:*:*:*"
    netid_pattern = oui.upper()

    while len(networks) < max_results:
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

        result = client.search_wifi(**kwargs)

        if not result.get("success", False):
            error = result.get("error", "unknown")
            if error == "rate_limited":
                print(f"    Rate limited after {len(networks)} results — stopping this OUI")
                break
            elif error == "unauthorized":
                break
            # Retry once on transient errors
            time.sleep(RATE_LIMIT_DELAY * 2)
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
        print(f"    Page {page}: +{len(results)} results  (total: {len(networks)}, "
              f"API reports: {total_count})")

        # Pagination
        search_after = result.get("searchAfter")
        if not search_after or len(results) < PAGE_SIZE:
            break

        # Rate limiting
        time.sleep(RATE_LIMIT_DELAY)

    return networks


def deduplicate_networks(networks: list) -> list:
    """Remove duplicate entries by netid (BSSID)."""
    seen = set()
    unique = []
    for net in networks:
        nid = net["netid"].upper()
        if nid not in seen:
            seen.add(nid)
            unique.append(net)
    return unique


# ─── Output Generators ───────────────────────────────────────────────────────

def write_geojson(networks: list, output_path: Path) -> None:
    """Write networks as GeoJSON FeatureCollection for the web map."""
    features = []
    for net in networks:
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
                "netid": net["netid"],
                "ssid": net.get("ssid", ""),
                "oui": net.get("oui_match", net["netid"][:8]),
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


def write_csv(networks: list, output_path: Path) -> None:
    """Write networks as CSV for data analysis."""
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


def write_stats(networks: list, ouis_queried: int, api_requests: int,
                output_path: Path) -> None:
    """Write scan statistics JSON."""
    now = datetime.now(timezone.utc)

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
        "unique_ouis_found": len(oui_counts),
        "ouis_queried": ouis_queried,
        "api_requests": api_requests,
        "cameras_by_oui": dict(sorted(oui_counts.items(), key=lambda x: -x[1])),
        "cameras_by_region": dict(sorted(region_counts.items(), key=lambda x: -x[1])[:50]),
        "cameras_by_country": dict(sorted(country_counts.items(), key=lambda x: -x[1])),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"  [✓] Stats: {output_path}")


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
        help="Bounding box as lat1,lon1,lat2,lon2 (e.g., '37,-97,39,-94' for Kansas City area)"
    )
    parser.add_argument(
        "--max-per-oui", type=int, default=MAX_RESULTS_PER_OUI,
        help=f"Max results per OUI prefix (default: {MAX_RESULTS_PER_OUI})"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Output directory (default: data/)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Verify auth and print OUI list without querying"
    )
    args = parser.parse_args()

    # ── Banner ────────────────────────────────────────────────────────────────
    banner = "=" * 62
    print(banner)
    print("  Flock Finder — WiGLE ALPR Camera Mapper")
    print("  OUI research: @NitekryDPaul  |  Inspired by DeFlock")
    print(banner)

    # ── Load credentials ──────────────────────────────────────────────────────
    print("\n[1/5] Loading credentials…")
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
    print("\n[2/5] Authenticating with WiGLE…")
    client = WiGLEClient(api_name, api_token)

    if not client.verify_auth():
        sys.exit(1)

    # ── Load OUI list ─────────────────────────────────────────────────────────
    print("\n[3/5] Loading Flock Safety OUI list…")

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

    if args.dry_run:
        print("\n  [DRY RUN] Would query these OUIs:")
        for oui in ouis:
            print(f"    • {oui}")
        print(f"\n  Total: {len(ouis)} OUI prefixes")
        sys.exit(0)

    # ── Query WiGLE ───────────────────────────────────────────────────────────
    print(f"\n[4/5] Querying WiGLE for {len(ouis)} OUI prefixes…")
    print(f"  Rate limit: {RATE_LIMIT_DELAY}s between requests")
    print(f"  Max results per OUI: {args.max_per_oui}")
    print()

    all_networks = []
    ouis_with_results = 0

    for i, oui in enumerate(ouis, 1):
        print(f"  [{i:2d}/{len(ouis)}] Querying OUI {oui}…")

        networks = query_oui(
            client, oui,
            country=args.country,
            bbox=bbox,
            max_results=args.max_per_oui,
        )

        if networks:
            ouis_with_results += 1
            all_networks.extend(networks)
            print(f"         → {len(networks)} cameras found")
        else:
            print(f"         → no results")

        # Rate limit between OUI queries
        if i < len(ouis):
            time.sleep(RATE_LIMIT_DELAY)

    # Deduplicate
    print(f"\n  Total raw results: {len(all_networks)}")
    all_networks = deduplicate_networks(all_networks)
    print(f"  After dedup:       {len(all_networks)}")

    # ── Write outputs ─────────────────────────────────────────────────────────
    print(f"\n[5/5] Writing output files…")

    output_dir = Path(args.output_dir) if args.output_dir else DATA_DIR
    geojson_out = output_dir / "flock_cameras.geojson"
    csv_out = output_dir / "flock_cameras.csv"
    stats_out = output_dir / "scan_stats.json"

    write_geojson(all_networks, geojson_out)
    write_csv(all_networks, csv_out)
    write_stats(all_networks, len(ouis), client.request_count, stats_out)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(banner)
    print(f"  Scan Complete!")
    print(banner)
    print(f"  Total cameras found : {len(all_networks)}")
    print(f"  OUIs with results   : {ouis_with_results}/{len(ouis)}")
    print(f"  API requests made   : {client.request_count}")
    print(f"  GeoJSON             : {geojson_out}")
    print(f"  CSV                 : {csv_out}")
    print(f"  Stats               : {stats_out}")
    print(banner)
    print()
    print("  Open docs/index.html to view the interactive map.")
    print("  Or run: python3 -m http.server 8080 --directory docs/")


if __name__ == "__main__":
    main()
