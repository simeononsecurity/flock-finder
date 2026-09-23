# 📡 Flock Finder

**Map Flock Safety ALPR surveillance cameras using WiGLE WiFi data and OUI fingerprinting.**

An open-source project that queries the [WiGLE](https://wigle.net) crowdsourced WiFi database for networks matching known Flock Safety camera OUI (MAC address) prefixes, then plots them on an interactive map.

Inspired by [DeFlock](https://www.deflock.me) and [track-openroaming-passpoint](https://github.com/simeononsecurity/track-openroaming-passpoint).

[![Dependency Graph](https://github.com/simeononsecurity/flock-finder/actions/workflows/dependabot/update-graph/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/dependabot/update-graph) [![pages-build-deployment](https://github.com/simeononsecurity/flock-finder/actions/workflows/pages/pages-build-deployment/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/pages/pages-build-deployment) [![Update Flock Camera Data](https://github.com/simeononsecurity/flock-finder/actions/workflows/update-data.yml/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/update-data.yml)

<!-- STATS_START -->
| Metric | Value |
|--------|-------|
| 📸 **Cameras Mapped** (actionable) | 14,968 |
| 🔎 *of which SSID-confirmed* | 1,223 |
| 🛰️ *of which OUI-suspected (high tier)* | 13,745 |
| 🧩 *of which OUI-suspected (contract-mfr tier)* | 0 |
| 🚫 **Excluded — SSID is other hardware** | 13,829 |
| 🌍 **Flagged outside the US** | 13,931 |
| 📡 **OUI Prefixes with Data** | 7 / 39 |
| 🌎 **Countries** | 101 |
| 🗺️ **Regions / provinces (distinct)** | 835 |
| 🕐 **Last Updated** | 2026-09-23 |
| 📦 **Data Retention** | 730 days (2 years) |
<!-- STATS_END -->

> *Stats update automatically after each scan via GitHub Actions. “Cameras Mapped” counts only the records that pass evidence classification — the breakdown is below.*

<!-- CONFIDENCE_BREAKDOWN_START -->
### What the numbers mean

Every published record carries a `confidence` field, because an OUI match on its own is weak evidence. The map and the headline count only the records that survive that classification.

| Confidence | Records | Share | Meaning |
|------------|---------|-------|---------|
| `ssid_confirmed` | 1,223 | 4.2% | SSID is a Flock naming pattern (`Flock`, `Flock-XXXXXX`, `Flock Camera net.`, `FS Ext Battery`) — strongest signal passive WiFi can give |
| `oui_high` | 13,745 | 47.7% | High-confidence Flock OUI, SSID absent/hidden/unrecognised — suspected, unverified |
| `oui_mfr` | 0 | 0.0% | Contract-manufacturer OUI (Liteon/USI) — weakest evidence, expect false positives |
| `identified_other` | 13,829 | 48.0% | SSID positively identifies other hardware — excluded from the map and from the camera count above |
| **All records** | **28,797** | 100.0% | |

SSID denylist hits: `clickshare*` 13,748 · `direct-*` 73 · `detector_verdict*` 8.

**Detector output ingested as data.** 8 records carry another detector's verdict string in the SSID column (`Flock ALPR [wifi_receiver_oui;low]` and similar) — self-tagged `low` 8 by the tool that produced them. They are a downstream copy of a detection, not an observation of a camera, so they are excluded outright instead of being counted in the SSID-confirmed subset.

**Per-prefix signal, measured.** Of the 7 prefixes with ≥200 records, the ones whose records mostly name *other* hardware:

| OUI prefix | Records | SSID-confirmed | Excluded as other hardware |
|------------|---------|----------------|----------------------------|
| `74:4C:A1` | 11,884 | 217 | 8,384 (71%) |
| `D8:F3:BC` | 3,969 | 159 | 2,544 (64%) |
| `14:5A:FC` | 4,049 | 270 | 2,414 (60%) |

The most Flock-confirmed, for contrast: `70:C9:4E` (27% SSID-confirmed, 135 of 498 records), `3C:91:80` (15% SSID-confirmed, 141 of 918 records), `80:30:49` (11% SSID-confirmed, 92 of 826 records).

The `tier` field in `data/flock_ouis.csv` is the curated (firmware-parity) judgement; these numbers are the measurement. Where they disagree, the measurement is the honest summary of what this dataset actually contains.

Market: 13,931 records (48.4%) are outside the primary US market and carry `"out_of_market": true`. They are flagged, not deleted — Flock has expanded internationally — so consumers can filter them.

Every record is still *suspected*: the tiers describe the strength of the evidence, not a confirmation from Flock that a device is theirs. See [docs/DATA_POLICY.md](docs/DATA_POLICY.md).
<!-- CONFIDENCE_BREAKDOWN_END -->

---

> [!WARNING]
> **Take this map with a grain of salt.** WiGLE is a crowdsourced, passively-collected dataset that is updated sporadically on a per-location basis — it is **not** a live feed. Flock cameras **do not broadcast continuously**; they wake briefly only to upload data, meaning WiGLE records depend entirely on someone happening to be wardriving in the right place at the right time. Locations may be stale, incomplete, or reflect cameras that have since been moved or removed.
>
> **This dashboard is a general awareness tool, not a source of truth.** For accurate, real-time, local detection use the hardware devices by [STSCollective](https://stscollective.com) described below — they implement @NitekryDPaul's actual detection method directly on an ESP32 and can detect Flock cameras as you drive past them. Use discount code **`FLOCKFINDER`** at checkout for **20% off** your order.

---

## 🔍 How It Works

Flock Safety ALPR cameras have WiFi transceivers that periodically wake to upload captured license plate data. These transmissions use MAC addresses with identifiable **OUI** (Organizationally Unique Identifier) prefixes.

**@NitekryDPaul** discovered 30 of these OUI prefixes through promiscuous-mode 2.4 GHz analysis. A 31st was contributed by **Michael / DeFlockJoplin** during field testing in Joplin, MO. Seven more have since been promoted into the canonical list:

- **`B4:1E:52`** — IEEE registration held directly by **Flock Safety** (the most authoritative signal in the list: it is Flock's own assignment, not a chipset vendor's).
- **`04:0D:84`, `F0:82:C0`, `1C:34:F1`, `38:5B:44`, `94:34:69`, `B4:E3:F9`** — the **FS Ext Battery** accessory / battery-pack series ([dougborg/PR#39](https://github.com/colonelpanichacks/flock-you/pull/39)). These packs broadcast an `FS Ext Battery…` SSID, which is why that naming pattern is now queried alongside `Flock%` / `FLOCK%`.

This project:
1. Takes those 39 known Flock Safety WiFi OUI prefixes
2. Queries the WiGLE WiFi database for networks matching each prefix
3. Deduplicates and exports results as GeoJSON + CSV
4. Displays camera locations on a dark-themed interactive Leaflet map

> **Note:** WiGLE is a historical, crowdsourced WiFi survey database — it does **not** use @NitekryDPaul's active detection technique. WiGLE entries are submitted by volunteers wardriving with passive scanners, so coverage is uneven and timestamps may be months or years old. The map is best used as a rough geographic reference, not a definitive or current inventory.

### Detection Strategy (from @NitekryDPaul's research)

Flock cameras spend most of their duty cycle **asleep**, waking briefly to upload. The key insight is matching on `addr1` (receiver/destination) in addition to `addr2` (transmitter) — revealing devices that a transmitter-only sniff would miss.

Combined with wildcard probe request detection (802.11 management frames type=0 subtype=4 with empty SSID), this yields a very tight signature: **11 of 12 cameras caught with only 2 false positives** in field testing.

> **This is the gold-standard detection method — and it requires dedicated hardware running in the field.** The WiGLE-based map in this repo does *not* implement addr1 matching; it can only see what WiGLE volunteers have already passively logged. For real-time, on-the-ground detection using this exact technique, see the **[STSCollective FlockYou devices](https://stscollective.com)** — ESP32-based detectors that scan for Flock OUI signatures as you drive, with LED and/or audio alerts the moment a camera is detected. Use discount code **`FLOCKFINDER`** at checkout for **20% off** your order.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- A [WiGLE account](https://wigle.net/account) with API credentials

### Setup

```bash
# Clone the repo
git clone https://github.com/simeononsecurity/flock-finder.git
cd flock-finder

# Install dependencies
pip install -r requirements.txt

# Create your .env file with WiGLE credentials (NOT tracked by git)
cp .env.example .env
# Edit .env with your API Name and Token from https://wigle.net/account
```

### Run the Scanner

```bash
# Full scan — all 39 OUI prefixes, worldwide
python3 scripts/wigle_query.py

# Single OUI test
python3 scripts/wigle_query.py --oui 70:C9:4E

# US only
python3 scripts/wigle_query.py --country US

# Specific region (bounding box: lat1,lon1,lat2,lon2)
python3 scripts/wigle_query.py --bbox 37,-97,39,-94

# Dry run — verify auth, print OUI list, no API queries
python3 scripts/wigle_query.py --dry-run
```

### View the Map

```bash
# Serve the docs directory locally
python3 -m http.server 8080 --directory docs/

# Open in browser
open http://localhost:8080
```

Or just open `docs/index.html` directly in your browser.

---

## 📁 Project Structure

```
flock-finder/
├── .env                  # WiGLE API credentials (gitignored)
├── .env.example          # Template for .env
├── .gitignore
├── README.md
├── requirements.txt
├── scripts/
│   └── wigle_query.py    # WiGLE API query script
├── data/
│   ├── flock_ouis.csv    # 39 known Flock Safety OUI prefixes (canonical, tiers)
│   ├── flock_cameras.geojson  # GENERATED + published (not committed) — see “Where Is the Data?”
│   ├── flock_cameras.csv      # GENERATED + published (not committed)
│   ├── by_oui/                # GENERATED per-OUI splits (not committed)
│   └── scan_stats.json        # Output: scan + evidence-tier statistics
├── docs/
│   └── index.html        # Interactive web map (Leaflet + dark theme)
└── .github/
    └── workflows/
        └── update-data.yml  # GitHub Actions: daily auto-update
```

---

## 📡 Flock Safety WiFi OUI Prefixes

39 known prefixes identified by **@NitekryDPaul** + **DeFlockJoplin** + **dougborg/PR#39**:

| # | OUI Prefix | Source |
|---|------------|--------|
| 1 | `70:C9:4E` | @NitekryDPaul |
| 2 | `3C:91:80` | @NitekryDPaul |
| 3 | `D8:F3:BC` | @NitekryDPaul |
| 4 | `80:30:49` | @NitekryDPaul |
| 5 | `B8:35:32` | @NitekryDPaul |
| 6 | `14:5A:FC` | @NitekryDPaul |
| 7 | `74:4C:A1` | @NitekryDPaul |
| 8 | `08:3A:88` | @NitekryDPaul |
| 9 | `9C:2F:9D` | @NitekryDPaul |
| 10 | `C0:35:32` | @NitekryDPaul |
| 11 | `94:08:53` | @NitekryDPaul |
| 12 | `E4:AA:EA` | @NitekryDPaul |
| 13 | `F4:6A:DD` | @NitekryDPaul |
| 14 | `F8:A2:D6` | @NitekryDPaul |
| 15 | `24:B2:B9` | @NitekryDPaul |
| 16 | `00:F4:8D` | @NitekryDPaul |
| 17 | `D0:39:57` | @NitekryDPaul |
| 18 | `E8:D0:FC` | @NitekryDPaul |
| 19 | `E0:4F:43` | @NitekryDPaul |
| 20 | `B8:1E:A4` | @NitekryDPaul |
| 21 | `70:08:94` | @NitekryDPaul |
| 22 | `58:8E:81` | @NitekryDPaul |
| 23 | `EC:1B:BD` | @NitekryDPaul |
| 24 | `3C:71:BF` | @NitekryDPaul |
| 25 | `58:00:E3` | @NitekryDPaul |
| 26 | `90:35:EA` | @NitekryDPaul |
| 27 | `5C:93:A2` | @NitekryDPaul |
| 28 | `64:6E:69` | @NitekryDPaul |
| 29 | `48:27:EA` | @NitekryDPaul |
| 30 | `A4:CF:12` | @NitekryDPaul |
| 31 | `82:6B:F2` | DeFlockJoplin |
| 32 | `B4:1E:52` | dougborg/PR#39 — direct IEEE registration to Flock Safety |
| 33 | `04:0D:84` | dougborg/PR#39 — FS Ext Battery series |
| 34 | `F0:82:C0` | dougborg/PR#39 — FS Ext Battery series |
| 35 | `1C:34:F1` | dougborg/PR#39 — FS Ext Battery series |
| 36 | `38:5B:44` | dougborg/PR#39 — FS Ext Battery series |
| 37 | `94:34:69` | dougborg/PR#39 — FS Ext Battery series |
| 38 | `B4:E3:F9` | dougborg/PR#39 — FS Ext Battery series |
| 39 | `E0:0A:F6` | dougborg/PR#39 — contract-manufacturer silicon (mfr tier) |

> **Reconciled with [flock-you-esp32](https://github.com/simeononsecurity/flock-you-esp32).** That firmware carries 41 prefixes; this list carries 39. The two deliberate omissions are `00:03:7F` (generic Qualcomm Atheros QCA9377 — see [the data policy](docs/DATA_POLICY.md#7-signatures-we-deliberately-do-not-query-and-why)) and `D4:11:D6` (SoundThinking/ShotSpotter, a different device class that the firmware alerts on but which is not a Flock camera). The remaining delta that this list *was* missing — `E0:0A:F6` — is now included.
>
> The per-prefix `vendor` column is the IEEE registrant, which is usually **not** Flock Safety: the hardware here runs on Liteon, Silicon Labs, USI and Espressif radio silicon, which is precisely why an OUI match needs the tier and the measured signal column.

---

## 📶 Top 10 Observed SSID Name Variants

The table below lists the ten most frequently observed SSID values across all WiGLE records matching a known Flock Safety OUI prefix. Because most Flock cameras transmit a **hidden SSID** (empty broadcast), the entries below represent the minority of records where a network name was visible to the WiGLE wardrive scanner. They are published here as a community reference for researchers and detection tool authors.

> ⚠️ **These SSIDs are not exclusive to Flock cameras.** They appear in records whose OUI prefix matches the known Flock Safety list — but many of these network names (e.g., `ClickShare`, `TEST`) are generic and may simply co-occupy the same MAC space. Treat them as correlated observations, not confirmed Flock identifiers.

<!-- SSID_TOP10_START -->
| # | SSID | Occurrences |
|---|------|-------------|
| 1 | `Flock` | 1,174 |
| 2 | `TEST` | 100 |
| 3 | `ClickShare-Boardroom` | 75 |
| 4 | `ClickShare` | 64 |
| 5 | `ClickShare-Conference Room` | 45 |
| 6 | `Afsol Wifi` | 29 |
| 7 | `VIAGO2_AP` | 24 |
| 8 | `<hidden>` | 23 |
| 9 | `DIRECT-` | 22 |
| 10 | `(no SSID)` | 20 |

*Computed from 24,141 SSID-bearing records (21,685 unique values) across all 7 OUI files in `data/by_oui/`. Stats update automatically after each scan.*
<!-- SSID_TOP10_END -->

---

## 🔬 Flock SSID Pattern Analysis

<!-- SSID_PATTERNS_START -->
Filtering for only `Flock*`-prefixed SSIDs yields **53 unique variants** across **1,231 total records**. These fall into five distinct patterns:

| Pattern | Unique SSIDs | Records | Description |
|---------|-------------|---------|-------------|
| `Flock` | 1 | 1,174 | Bare name — fully configured / deployed cameras |
| `Flock-XXXXXX` | 41 | 41 | Mixed-case with 6-char uppercase hex suffix |
| `FLOCK-XXXXXX` | 5 | 5 | All-caps variant with 6-char hex suffix |
| `Flock-XXXX` | 1 | 1 | Shorter 4-char hex suffix (`Flock-6361`) |
| `FlockXXX` | 2 | 2 | Numeric suffix, no dash (`Flock001`, `Flock003`) |
| Other | 3 | 8 | Other / non-standard patterns |

**The `Flock-XXXXXX` / `FLOCK-XXXXXX` naming convention is consistent with camera provisioning SSIDs** — each device appears to broadcast a unique hex identifier (likely derived from its MAC address) before being claimed and configured through the Flock Safety platform. Once provisioned, the SSID collapses to the bare `Flock` name.

> This pattern is a strong secondary confirmation signal: observing a `Flock-XXXXXX` SSID on a matching OUI prefix is highly indicative of an unconfigured or recently factory-reset Flock Safety camera.
<!-- SSID_PATTERNS_END -->

---

## 🔭 SSID-Discovery: Candidate OUI Prefixes (Unconfirmed)

> [!NOTE]
> **New SSID pattern observed in the wild — `Flock Camera net.`**
> A community member ([flock-you issue #43](https://github.com/colonelpanichacks/flock-you/issues/43)) reported a Flock Safety camera broadcasting as **`Flock Camera net.`** — a naming format completely invisible to `Flock-*` pattern searches. The same camera simultaneously broadcast on both **2.4 GHz (channel 1)** and **5 GHz (channel 157)** using **sequential locally administered MACs** (e.g. `52:64:CF:9F:A2:DE` / `:DF`). The locally administered addressing is likely a **deliberate anti-fingerprinting measure** — these MACs will never match IEEE OUI lookups, making SSID-pattern detection the only viable passive identification path for cameras using this scheme.

This section is populated automatically by querying WiGLE for any SSID matching `Flock%`, `FLOCK%` or `FS Ext Battery%`, then extracting OUI prefixes from the results that are **not** in the canonical `flock_ouis.csv`. Only OUIs observed **≥5 times** are reported here to reduce false positives.

<!-- CANDIDATE_OUIS_START -->
*No candidate OUI prefixes identified yet — SSID-pattern incremental queries are running. Candidate prefixes will appear here once any novel OUI is observed ≥5 times in Flock-SSID-bearing WiGLE records.*
<!-- CANDIDATE_OUIS_END -->

<!-- SSID_COVERAGE_START -->
### Camera-class coverage

**LAA-MAC camera class (`Flock Camera net.`): zero records.** No published record has an SSID containing `Flock Camera net.`, so the one camera class that defeats OUI matching entirely (locally-administered MACs — [flock-you issue #43](https://github.com/colonelpanichacks/flock-you/issues/43)) currently has **no coverage at all**. This is a *data* gap, not a pattern gap: the `Flock%` pattern is a prefix match that already covers `Flock Camera net.`, so the search is correct — either no wardrive has passed one of these cameras yet, or they broadcast a hidden SSID. If it stays at zero across several scans, the next widening step is `%Camera net.%` (any prefix before `Camera`), which trades precision for coverage.

**Locally-administered (LAA) records:** none in the published dataset.

**FS Ext Battery pattern:** 0 published record(s) carry an `FS Ext Battery…` SSID — the class the `FS Ext Battery%` discovery pattern was added for.

| SSID pattern | Records matched (this pass) |
|--------------|------------------------------|
| `Flock%` | 0 |
| `FLOCK%` | 0 |
| `FS Ext Battery%` | 0 |
| `fs ext battery%` | 0 |
<!-- SSID_COVERAGE_END -->

---

## ⚙️ GitHub Actions (Automated Updates)

The included workflow runs daily and auto-commits updated camera data:

1. Add your WiGLE credentials as **repository secrets**:
   - `WIGLE_API_NAME` — your API name from wigle.net/account
   - `WIGLE_API_TOKEN` — your API token

2. The workflow runs at 6 AM UTC daily, or manually via "Run workflow"

3. If new data is found, it commits updated GeoJSON/CSV/stats automatically

---

## 📦 Where Is the Data?

The dataset is **published, not committed**. `flock_cameras.geojson` (~90 MB,
rewritten in full on every daily scan), `flock_cameras.csv` and the per-OUI
splits under `data/by_oui/` used to be committed; because each scan rewrites them
completely, they grew the repository to ~1.6 GB and made it slow to clone. They
now live in three places instead:

| What | Where |
|------|-------|
| Live map (unchanged `/data/…` paths) | the [GitHub Pages site](https://simeononsecurity.github.io/flock-finder/) — the deploy jobs restore the files from the release before publishing |
| Downloadable dataset | the rolling [`data-latest` release](https://github.com/simeononsecurity/flock-finder/releases/tag/data-latest): `flock_cameras.geojson`, `flock_cameras.csv`, `by_oui.zip` |
| A local checkout | run `python3 scripts/wigle_query.py` (needs WiGLE credentials), or fetch the published copy: `gh release download data-latest -p 'flock_cameras.geojson' -D data` |

What stays in git are the small, diffable artifacts: `data/flock_ouis.csv` (the
canonical OUI list), `scan_stats.json`, `candidate_ouis.json` and
`ssid_candidate_cameras.*`.

> **History note.** Untracking stops the growth; the old blobs are still in
> history. Reclaiming the existing ~1.6 GB requires a history rewrite
> (`git filter-repo --path data/flock_cameras.geojson --path data/flock_cameras.csv --path data/by_oui --invert-paths`),
> which is a maintainer call because it rewrites every commit hash.

---

## 🔒 API Key Security

- The `.env` file containing your WiGLE API credentials is **gitignored** — it will never be committed
- For GitHub Actions, credentials are stored as **repository secrets** (encrypted)
- Never commit API keys to the repository

---

## 🙏 Credits

- **OUI Research:** [@NitekryDPaul](https://github.com/NitekryDPaul) — all 30 original OUI prefixes and the promiscuous-mode detection strategy
- **Field Testing:** [DeFlockJoplin](https://github.com/DeflockJoplin/flock-you) — 31st OUI prefix (`82:6B:F2`) and wildcard probe tightening
- **Additional OUI prefixes:** [dougborg/PR#39](https://github.com/colonelpanichacks/flock-you/pull/39) — Flock Safety's own IEEE registration (`B4:1E:52`) and the six-prefix FS Ext Battery series
- **Inspired by:** [DeFlock](https://www.deflock.me) (ALPR mapping) and [track-openroaming-passpoint](https://github.com/simeononsecurity/track-openroaming-passpoint) (WiGLE data mining)
- **Data Source:** [WiGLE](https://wigle.net) — crowdsourced WiFi/cell network database
- **Map:** [Leaflet](https://leafletjs.com) + [OpenStreetMap](https://www.openstreetmap.org)

---

## 📚 Documentation

- **[Data Policy, Provenance & Corrections](docs/DATA_POLICY.md)** — every record is *suspected* (OUI match only), coordinates are published at full precision, and here's how to request a correction.
- **[Data Dictionary](docs/DATA_DICTIONARY.md)** — field-level schema for every file under `data/`.
- **[Contributing Guide](CONTRIBUTING.md)** — local setup, lint/test/validate commands, and how to add an OUI prefix.

> ⚠️ **These are *suspected* Flock devices, not confirmed.** An OUI match is a heuristic — OUIs can be shared, reassigned, or spoofed. Coordinates are published at full precision, exactly as WiGLE reports them. See the [Data Policy](docs/DATA_POLICY.md).


---

## ⚖️ Legal & Ethics

This project uses only **publicly available data** from the WiGLE database, which aggregates voluntarily contributed WiFi survey data. No hacking, unauthorized access, or proprietary systems are involved.


The goal is **transparency** — communities have a right to know where surveillance infrastructure is deployed in their neighborhoods.

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.
