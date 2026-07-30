#!/usr/bin/env python3
"""
ssid_query.py — WiGLE SSID-Pattern Query for Novel Flock OUI Discovery
========================================================================
Queries WiGLE for networks whose SSID matches known Flock Safety naming
patterns (Flock%, FLOCK%).  For each result the OUI prefix is extracted and
compared against the canonical Flock OUI list (data/flock_ouis.csv).

OUI prefixes that are:
  1. NOT already in the known Flock OUI list, AND
  2. Seen ≥ CANDIDATE_MIN_COUNT times across all SSID-matched results

…are flagged as **candidate** prefixes and written to separate output files.

Output files
------------
  data/ssid_candidate_cameras.geojson  — locations of cameras with candidate OUIs
  data/ssid_candidate_cameras.csv      — same data in CSV form
  data/candidate_ouis.json             — candidate OUI list with evidence summary

The script also injects updated tables into README.md and docs/index.html
using HTML comment markers so the website always reflects the latest results.

Important notes
---------------
* Locally administered MACs (bit 1 of first octet = 1) are flagged separately.
  Per flock-you issue #43 (https://github.com/colonelpanichacks/flock-you/issues/43),
  Flock cameras may use locally administered addressing on their hotspot interface
  as an anti-fingerprinting measure; these will never match IEEE OUI lookups.

* All candidate OUI prefixes are UNCONFIRMED — they require field verification
  before being added to the canonical flock_ouis.csv.

Run manually:   python3 scripts/ssid_query.py
Via CI:         called automatically after wigle_query.py in update-data.yml
"""

import csv
import html as html_lib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Shared validation helpers
sys.path.insert(0, str(Path(__file__).parent))
from validation import is_valid_latlon  # noqa: E402

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent.absolute()
PROJECT_DIR  = SCRIPT_DIR.parent
DATA_DIR     = PROJECT_DIR / "data"
ENV_FILE     = PROJECT_DIR / ".env"
README_PATH  = PROJECT_DIR / "README.md"
INDEX_HTML   = PROJECT_DIR / "docs" / "index.html"

# ── Configuration ─────────────────────────────────────────────────────────────
WIGLE_API_BASE      = "https://api.wigle.net/api/v2"
WIGLE_SEARCH_EP     = f"{WIGLE_API_BASE}/network/search"
RATE_LIMIT_DELAY    = 2.5         # seconds between API pages
PAGE_SIZE           = 100
MAX_AGE_DAYS        = 730         # 2 years retention (same as main scan)
INCREMENTAL_OVERLAP = 1           # re-query 1 day overlap on incremental runs
CANDIDATE_MIN_COUNT = 5           # OUI must appear ≥ this many times to be reported

# Output files
CANDIDATE_GEOJSON    = DATA_DIR / "ssid_candidate_cameras.geojson"
CANDIDATE_CSV        = DATA_DIR / "ssid_candidate_cameras.csv"
CANDIDATE_OUIS_JSON  = DATA_DIR / "candidate_ouis.json"
SSID_STATE_FILE      = DATA_DIR / "ssid_scan_state.json"

# SSID patterns — WiGLE supports % as a SQL-style wildcard suffix
# These deliberately cast a wide net; novel-OUI filtering happens in analysis.
FLOCK_SSID_PATTERNS = [
    ("Flock%",  "Flock, Flock-XXXXXX, Flock Camera net., and all Flock-prefixed variants"),
    ("FLOCK%",  "FLOCK-XXXXXX all-caps variant"),
]


# ── MAC / OUI helpers ─────────────────────────────────────────────────────────

def extract_oui(mac: str) -> str:
    """Return the normalized OUI prefix (XX:XX:XX) from a MAC address string."""
    parts = mac.upper().replace("-", ":").split(":")
    if len(parts) >= 3:
        return ":".join(parts[:3])
    return ""


def is_locally_administered(mac: str) -> bool:
    """
    Return True when the MAC address has the locally administered bit set
    (bit 1 of the first octet, value 0x02).

    Locally administered MACs are intentionally assigned outside the IEEE
    OUI registry and will never appear in standard OUI lookups.
    """
    parts = mac.upper().replace("-", ":").split(":")
    if not parts:
        return False
    try:
        return bool(int(parts[0], 16) & 0x02)
    except ValueError:
        return False


# ── Known OUI loader ──────────────────────────────────────────────────────────

def load_known_ouis() -> set:
    """Load known Flock OUI prefixes from data/flock_ouis.csv → set of 'XX:XX:XX' strings."""
    known: set = set()
    oui_csv = DATA_DIR / "flock_ouis.csv"
    if not oui_csv.exists():
        print("  [!] flock_ouis.csv not found — all OUIs will be treated as novel")
        return known
    with open(oui_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("oui") or "").strip().upper().replace("-", ":")
            parts = raw.split(":")
            if len(parts) == 3:
                known.add(":".join(parts))
    return known


# ── Scan state persistence ────────────────────────────────────────────────────

def load_ssid_state() -> dict:
    """Load per-SSID-pattern incremental scan state from disk."""
    if not SSID_STATE_FILE.exists():
        return {}
    try:
        with open(SSID_STATE_FILE) as f:
            return json.load(f).get("patterns", {})
    except Exception as exc:
        print(f"  [!] Could not load SSID state: {exc}")
        return {}


def save_ssid_state(pattern_states: dict) -> None:
    """Atomically persist per-SSID-pattern scan state."""
    envelope = {
        "updated":     datetime.now(timezone.utc).isoformat(),
        "description": "Per-SSID-pattern incremental scan state for ssid_query.py. "
                       "Delete to force a full rescan.",
        "patterns":    pattern_states,
    }
    SSID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SSID_STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(envelope, f, indent=2)
    os.replace(tmp, SSID_STATE_FILE)


def compute_since_date(state: dict) -> str:
    """Derive the lastupdt filter date for a pattern based on its scan state."""
    if not state or state.get("status") == "never":
        return (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y%m%d")
    if state.get("status") == "interrupted":
        saved = state.get("since_date")
        if saved:
            return saved
    if state.get("status") == "completed":
        last = state.get("last_completed", "")
        if last:
            try:
                dt = datetime.fromisoformat(last) - timedelta(days=INCREMENTAL_OVERLAP)
                floor = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
                return max(dt, floor).strftime("%Y%m%d")
            except (ValueError, TypeError):
                pass
    return (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).strftime("%Y%m%d")


# ── WiGLE SSID query (paginated) ──────────────────────────────────────────────

def query_ssid_pattern(
    session: requests.Session,
    ssid_pattern: str,
    since_date: str = None,
    resume_cursor: str = None,
    resume_page: int = 0,
) -> tuple:
    """
    Query WiGLE for all WiFi networks whose SSID matches ssid_pattern.
    Paginates until no more results or rate-limited.

    Returns: (networks, rate_limited, final_cursor, final_page)
    """
    networks: list = []
    search_after = resume_cursor
    page = resume_page
    rate_limited = False

    while True:
        page += 1
        params: dict = {"ssid": ssid_pattern, "resultsPerPage": PAGE_SIZE}
        if search_after:
            params["searchAfter"] = search_after
        if since_date:
            params["lastupdt"] = since_date

        try:
            resp = session.get(WIGLE_SEARCH_EP, params=params, timeout=30)
        except requests.Timeout:
            print(f"    [!] Timeout on page {page} — stopping pagination")
            break
        except Exception as exc:
            print(f"    [!] Request error: {exc}")
            break

        if resp.status_code == 429:
            print(f"    ⚠ Rate limited after {len(networks)} results")
            rate_limited = True
            break
        if resp.status_code == 401:
            print("    [✗] 401 Unauthorized — check API credentials")
            break
        if resp.status_code == 404:
            break  # WiGLE returns 404 for empty result sets
        if resp.status_code != 200:
            print(f"    [!] HTTP {resp.status_code} — stopping")
            break

        data = resp.json()
        if not data.get("success", False):
            break

        results = data.get("results", [])
        if not results:
            break

        for net in results:
            networks.append({
                "netid":        net.get("netid", ""),
                "ssid":         net.get("ssid", ""),
                "trilat":       net.get("trilat"),
                "trilong":      net.get("trilong"),
                "channel":      net.get("channel"),
                "encryption":   net.get("encryption", ""),
                "firsttime":    net.get("firsttime", ""),
                "lasttime":     net.get("lasttime", ""),
                "city":         net.get("city", ""),
                "region":       net.get("region", ""),
                "country":      net.get("country", ""),
                "road":         net.get("road", ""),
                "postalcode":   net.get("postalcode", ""),
                "ssid_pattern": ssid_pattern,
            })

        total = data.get("totalResults", data.get("resultCount", 0))
        print(f"    Page {page}: +{len(results)} results  "
              f"(running: {len(networks)}, API total: {total})")

        search_after = data.get("searchAfter")
        if not search_after or len(results) < PAGE_SIZE:
            break

        time.sleep(RATE_LIMIT_DELAY)

    return networks, rate_limited, search_after, page


# ── Candidate OUI analysis ────────────────────────────────────────────────────

def analyze_candidate_ouis(networks: list, known_ouis: set) -> dict:
    """
    Identify OUI prefixes in SSID-matched WiGLE results that are NOT in the
    known Flock OUI list and appear ≥ CANDIDATE_MIN_COUNT times.

    Returns a dict keyed by OUI prefix, sorted by descending occurrence count.
    """
    oui_info: dict = defaultdict(lambda: {
        "count":                0,
        "is_locally_administered": False,
        "ssids":                Counter(),
        "example_macs":         set(),
    })

    for net in networks:
        mac = net.get("netid", "")
        if not mac:
            continue
        oui = extract_oui(mac)
        if not oui or oui in known_ouis:
            continue
        oui_info[oui]["count"] += 1
        oui_info[oui]["is_locally_administered"] = is_locally_administered(mac)
        ssid = net.get("ssid", "")
        if ssid:
            oui_info[oui]["ssids"][ssid] += 1
        oui_info[oui]["example_macs"].add(mac)

    candidates = {}
    for oui, info in oui_info.items():
        if info["count"] < CANDIDATE_MIN_COUNT:
            continue
        top_ssids = [s for s, _ in info["ssids"].most_common(3)]
        candidates[oui] = {
            "oui":                    oui,
            "count":                  info["count"],
            "is_locally_administered": info["is_locally_administered"],
            "top_ssids":              top_ssids,
            "example_mac":            sorted(info["example_macs"])[0],
        }

    return dict(sorted(candidates.items(), key=lambda x: -x[1]["count"]))


# ── Output writers ────────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def write_candidate_geojson(networks: list, candidates: dict, path: Path) -> None:
    candidate_ouis = set(candidates.keys())
    features = []
    for net in networks:
        mac = net.get("netid", "")
        oui = extract_oui(mac)
        if oui not in candidate_ouis:
            continue
        lat, lon = net.get("trilat"), net.get("trilong")
        if not is_valid_latlon(lat, lon):
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": {
                "netid":             mac,
                "ssid":              net.get("ssid", ""),
                "oui":               oui,
                "match_confidence":  "unconfirmed_candidate",
                "is_locally_admin":  is_locally_administered(mac),
                "channel":           net.get("channel"),
                "encryption":        net.get("encryption", ""),
                "firsttime":         net.get("firsttime", ""),
                "lasttime":          net.get("lasttime", ""),
                "city":              net.get("city", ""),
                "region":            net.get("region", ""),
                "country":           net.get("country", ""),
                "road":              net.get("road", ""),
                "postalcode":        net.get("postalcode", ""),
                "detection_method":  "ssid_pattern_match",
            },
        })

    _atomic_write_json(path, {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "generated":        datetime.now(timezone.utc).isoformat(),
            "source":           "WiGLE (wigle.net)",
            "project":          "flock-finder",
            "description": (
                "CANDIDATE Flock Safety ALPR camera locations discovered via SSID-pattern "
                "WiGLE queries (Flock%, FLOCK%). OUI prefixes in this file are NOT in the "
                "canonical flock_ouis.csv — they appeared ≥5 times in SSID-matched results "
                "and require field verification. match_confidence = 'unconfirmed_candidate'."
            ),
            "match_confidence":  "unconfirmed_candidate",
            "total_cameras":     len(features),
            "candidate_ouis":    len(candidates),
        },
    })
    print(f"  [✓] Candidate GeoJSON : {path}  ({len(features)} features)")


def write_candidate_csv(networks: list, candidates: dict, path: Path) -> None:
    candidate_ouis = set(candidates.keys())
    records = [
        net for net in networks
        if extract_oui(net.get("netid", "")) in candidate_ouis
        and is_valid_latlon(net.get("trilat"), net.get("trilong"))
    ]
    records.sort(key=lambda n: n.get("netid", ""))
    fieldnames = [
        "netid", "ssid", "trilat", "trilong", "channel", "encryption",
        "firsttime", "lasttime", "city", "region", "country", "road", "postalcode",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    print(f"  [✓] Candidate CSV     : {path}  ({len(records)} rows)")


def write_candidate_ouis_json(candidates: dict, path: Path) -> None:
    _atomic_write_json(path, {
        "generated": datetime.now(timezone.utc).isoformat(),
        "description": (
            f"Candidate Flock Safety OUI prefixes identified via SSID-pattern WiGLE queries. "
            f"Each prefix appeared ≥{CANDIDATE_MIN_COUNT} times in Flock-SSID-bearing records "
            "but is NOT in the canonical flock_ouis.csv. "
            "Treat as unconfirmed leads requiring field verification."
        ),
        "min_count_threshold": CANDIDATE_MIN_COUNT,
        "total_candidates": len(candidates),
        "candidates": list(candidates.values()),
    })
    print(f"  [✓] Candidate OUIs    : {path}  ({len(candidates)} prefixes)")


# ── README / index.html injection ────────────────────────────────────────────

def _replace_markers(content: str, start: str, end: str, inner: str) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    result, n = pattern.subn(f"{start}\n{inner}\n{end}", content)
    if n == 0:
        raise ValueError(f"Marker not found: {start!r}")
    return result


def _la_note_md(la_count: int) -> str:
    if not la_count:
        return ""
    return (
        f"\n> ⚠️ **{la_count} candidate OUI(s) have the locally administered bit set** "
        "(marked ⚠️ LA below). Per "
        "[colonelpanichacks/flock-you#43](https://github.com/colonelpanichacks/flock-you/issues/43), "
        "Flock cameras may deliberately use locally administered MAC addresses on their hotspot "
        "interface as an anti-fingerprinting measure. These will **never** match IEEE OUI lookups, "
        "so SSID-pattern matching remains the only viable passive detection path for these devices.\n"
    )


def update_readme(candidates: dict, readme: Path) -> None:
    if not readme.exists():
        return
    la_count = sum(1 for v in candidates.values() if v["is_locally_administered"])

    if not candidates:
        inner = (
            f"*No candidate OUI prefixes identified yet — SSID-pattern incremental queries are "
            f"running. Candidate prefixes will appear here once any novel OUI is observed "
            f"≥{CANDIDATE_MIN_COUNT} times in Flock-SSID-bearing WiGLE records.*"
        )
    else:
        rows = []
        for oui, info in candidates.items():
            la = " ⚠️ LA" if info["is_locally_administered"] else ""
            ssids = ", ".join(f"`{s}`" for s in info["top_ssids"][:2])
            rows.append(f"| `{oui}`{la} | {info['count']:,} | {ssids} |")
        inner = (
            f"SSID-pattern WiGLE queries (`Flock%` / `FLOCK%`) identified "
            f"**{len(candidates)} candidate OUI prefix(es)** that appear "
            f"≥{CANDIDATE_MIN_COUNT} times in SSID-matched records but are **not** in "
            "the canonical `flock_ouis.csv`. These are **unconfirmed**.\n\n"
            "> 🔔 **Community help needed.** If you observe one of these OUI prefixes on a "
            "confirmed Flock camera in the field, please open an issue or PR to promote it to "
            "`flock_ouis.csv`.\n"
            f"{_la_note_md(la_count)}\n"
            "| OUI Prefix | Occurrences | Top SSIDs Seen |\n"
            "|------------|------------|----------------|\n"
            + "\n".join(rows) + "\n\n"
            "*Location data: [`data/ssid_candidate_cameras.geojson`](data/ssid_candidate_cameras.geojson) "
            "· [`data/ssid_candidate_cameras.csv`](data/ssid_candidate_cameras.csv). "
            "Updated automatically after each scan.*"
        )

    content = readme.read_text(encoding="utf-8")
    try:
        updated = _replace_markers(
            content, "<!-- CANDIDATE_OUIS_START -->", "<!-- CANDIDATE_OUIS_END -->", inner
        )
        readme.write_text(updated, encoding="utf-8")
        print(f"  [✓] README candidate OUI section updated ({len(candidates)} entries)")
    except ValueError as e:
        print(f"  [!] README candidate OUI markers missing — skipping ({e})")


def update_index_html(candidates: dict, html_path: Path) -> None:
    if not html_path.exists():
        return
    la_count = sum(1 for v in candidates.values() if v["is_locally_administered"])

    if not candidates:
        rows_html = (
            '                    <tr><td colspan="4" style="color:var(--text-muted);text-align:center;">'
            f'No candidate OUI prefixes yet — queries running incrementally. '
            f'Prefixes seen ≥{CANDIDATE_MIN_COUNT} times will appear here.</td></tr>'
        )
        summary_html = (
            '        <p>\n'
            '            No candidate OUI prefixes have been identified yet via SSID-pattern queries.\n'
            f'            Any novel OUI seen ≥{CANDIDATE_MIN_COUNT} times in\n'
            '            Flock-SSID-bearing WiGLE records will appear here automatically.\n'
            '        </p>'
        )
    else:
        row_parts = []
        for oui, info in candidates.items():
            la_badge = (
                ' <span style="color:var(--yellow);font-size:0.75rem;'
                'margin-left:0.3rem;" title="Locally administered MAC — see flock-you#43">⚠️ LA</span>'
                if info["is_locally_administered"] else ""
            )
            ssids = ", ".join(html_lib.escape(s) for s in info["top_ssids"][:2])
            row_parts.append(
                f'                    <tr>\n'
                f'                        <td><code>{html_lib.escape(oui)}</code>{la_badge}</td>\n'
                f'                        <td style="color:var(--yellow);font-weight:700;">'
                f'{info["count"]:,}</td>\n'
                f'                        <td style="color:var(--text-muted);">{ssids}</td>\n'
                f'                        <td><span style="background:rgba(255,214,0,0.12);'
                f'color:var(--yellow);font-size:0.75rem;padding:0.2rem 0.5rem;'
                f'border-radius:4px;white-space:nowrap;">unconfirmed</span></td>\n'
                f'                    </tr>'
            )
        rows_html = "\n".join(row_parts)

        la_html = ""
        if la_count:
            la_html = (
                '\n        <div style="background:rgba(255,214,0,0.06);border:1px solid '
                'rgba(255,214,0,0.25);border-radius:8px;padding:0.75rem 1rem;margin-top:0.75rem;'
                'font-size:0.875rem;color:#ccc;line-height:1.55;">'
                f'<strong style="color:#ffd600;">⚠️ {la_count} candidate OUI(s) have the '
                'locally administered bit set.</strong> '
                'Per <a href="https://github.com/colonelpanichacks/flock-you/issues/43" '
                'target="_blank" style="color:#ffd600;">flock-you issue #43</a>, Flock cameras '
                'may deliberately use locally administered MAC addresses on their hotspot '
                'interface as an anti-fingerprinting measure — these will never appear in '
                'IEEE OUI registries. SSID-pattern matching remains effective regardless of '
                'MAC addressing scheme.</div>'
            )

        summary_html = (
            '        <p>\n'
            '            SSID-pattern WiGLE queries (<code>Flock%</code> / <code>FLOCK%</code>)\n'
            f'            identified <strong>{len(candidates)} candidate OUI prefix(es)</strong> '
            f'seen ≥{CANDIDATE_MIN_COUNT} times\n'
            '            that are <strong>not</strong> in the known Flock Safety OUI list.\n'
            '            These are <strong>unconfirmed</strong> — community field verification needed.\n'
            f'        </p>{la_html}'
        )

    content = html_path.read_text(encoding="utf-8")
    try:
        updated = _replace_markers(
            content,
            "<!-- CANDIDATE_OUIS_SUMMARY_START -->",
            "<!-- CANDIDATE_OUIS_SUMMARY_END -->",
            summary_html,
        )
        updated = _replace_markers(
            updated,
            "<!-- CANDIDATE_OUIS_ROWS_START -->",
            "<!-- CANDIDATE_OUIS_ROWS_END -->",
            rows_html,
        )
        html_path.write_text(updated, encoding="utf-8")
        print(f"  [✓] index.html candidate OUI section updated ({len(candidates)} entries)")
    except ValueError as e:
        print(f"  [!] index.html candidate OUI markers missing — skipping ({e})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    banner = "=" * 62
    print(banner)
    print("  Flock Finder — SSID-Pattern OUI Discovery")
    print("  Detecting novel OUIs via Flock SSID patterns in WiGLE")
    print(banner)

    # ── Credentials ──────────────────────────────────────────────────────────
    print("\n[1/5] Loading credentials…")
    load_dotenv(ENV_FILE)
    api_name  = os.getenv("WIGLE_API_NAME")
    api_token = os.getenv("WIGLE_API_TOKEN")
    if not api_name or not api_token:
        print("  [✗] WiGLE API credentials not found. Set WIGLE_API_NAME + WIGLE_API_TOKEN.")
        sys.exit(1)

    session = requests.Session()
    session.auth = (api_name, api_token)
    session.headers.update({
        "Accept":     "application/json",
        "User-Agent": "flock-finder/1.0 (https://github.com/simeononsecurity/flock-finder)",
    })

    try:
        resp = session.get(f"{WIGLE_API_BASE}/profile/user", timeout=15)
        if resp.status_code != 200:
            print(f"  [✗] Auth failed: HTTP {resp.status_code}")
            sys.exit(1)
        print("  [✓] Authenticated successfully")
    except Exception as exc:
        print(f"  [✗] Connection error: {exc}")
        sys.exit(1)

    # ── Known OUI list ────────────────────────────────────────────────────────
    print("\n[2/5] Loading known Flock OUI list…")
    known_ouis = load_known_ouis()
    print(f"  [✓] {len(known_ouis)} known OUI prefixes loaded from flock_ouis.csv")

    # ── Scan state ────────────────────────────────────────────────────────────
    print("\n[3/5] Loading SSID scan state…")
    pattern_states = load_ssid_state()
    if pattern_states:
        done = sum(1 for s in pattern_states.values() if s.get("status") == "completed")
        interrupted = sum(1 for s in pattern_states.values() if s.get("status") == "interrupted")
        print(f"  [✓] {done} completed, {interrupted} interrupted")
    else:
        print("  No prior state — full scan for all patterns")

    # ── SSID queries ──────────────────────────────────────────────────────────
    print(f"\n[4/5] Querying WiGLE for {len(FLOCK_SSID_PATTERNS)} SSID pattern(s)…")
    all_networks: dict = {}  # keyed by uppercase netid for dedup

    for pat, description in FLOCK_SSID_PATTERNS:
        key = pat.replace("%", "_pct")
        state = pattern_states.get(key, {})
        since = compute_since_date(state)
        is_resume = state.get("status") == "interrupted"
        cursor = state.get("search_after") if is_resume else None
        page0  = state.get("page", 0) if is_resume else 0
        label  = ("interrupted — resuming" if is_resume
                  else ("incremental" if state.get("status") == "completed" else "first scan"))

        print(f"\n  Pattern : {pat!r}")
        print(f"  Match   : {description}")
        print(f"  Mode    : {label}  (lastupdt≥{since})")

        networks, rate_limited, final_cursor, final_page = query_ssid_pattern(
            session, pat,
            since_date=since,
            resume_cursor=cursor,
            resume_page=page0,
        )

        # Dedup by netid — keep latest lasttime
        for net in networks:
            netid = net.get("netid", "").upper()
            if not netid:
                continue
            existing = all_networks.get(netid)
            if existing is None or net.get("lasttime", "") >= existing.get("lasttime", ""):
                all_networks[netid] = net

        print(f"  → fetched {len(networks)}, dedup pool now {len(all_networks)}")

        now_iso = datetime.now(timezone.utc).isoformat()
        if rate_limited:
            pattern_states[key] = {
                "status":       "interrupted",
                "search_after": final_cursor,
                "page":         final_page,
                "since_date":   since,
                "interrupted_at": now_iso,
            }
            print("  ⚠ Rate limited — state saved, will resume next run")
        else:
            pattern_states[key] = {
                "status":         "completed",
                "last_completed": now_iso,
                "search_after":   None,
                "page":           final_page,
                "since_date":     since,
            }

        save_ssid_state(pattern_states)
        time.sleep(RATE_LIMIT_DELAY)

    # ── Candidate OUI analysis ────────────────────────────────────────────────
    print(f"\n[5/5] Analyzing {len(all_networks)} records for novel OUI prefixes…")
    networks_list = list(all_networks.values())
    candidates = analyze_candidate_ouis(networks_list, known_ouis)

    print(f"  Novel OUIs with ≥{CANDIDATE_MIN_COUNT} records: {len(candidates)}")
    for oui, info in candidates.items():
        la = " [locally administered]" if info["is_locally_administered"] else ""
        print(f"    {oui}{la}: {info['count']:,} records — SSIDs: {info['top_ssids'][:2]}")

    # ── Write outputs ─────────────────────────────────────────────────────────
    write_candidate_geojson(networks_list, candidates, CANDIDATE_GEOJSON)
    write_candidate_csv(networks_list, candidates, CANDIDATE_CSV)
    write_candidate_ouis_json(candidates, CANDIDATE_OUIS_JSON)
    update_readme(candidates, README_PATH)
    update_index_html(candidates, INDEX_HTML)

    print()
    print(banner)
    print("  SSID-Pattern Scan Complete!")
    print(banner)
    print(f"  Records collected       : {len(all_networks)}")
    print(f"  Candidate OUIs (≥{CANDIDATE_MIN_COUNT})   : {len(candidates)}")
    print(f"  Candidate GeoJSON       : {CANDIDATE_GEOJSON}")
    print(f"  Candidate CSV           : {CANDIDATE_CSV}")
    print(f"  Candidate OUIs JSON     : {CANDIDATE_OUIS_JSON}")
    print(banner)


if __name__ == "__main__":
    main()
