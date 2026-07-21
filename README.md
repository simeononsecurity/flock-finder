# 📡 Flock Finder

**Map Flock Safety ALPR surveillance cameras using WiGLE WiFi data and OUI fingerprinting.**

An open-source project that queries the [WiGLE](https://wigle.net) crowdsourced WiFi database for networks matching known Flock Safety camera OUI (MAC address) prefixes, then plots them on an interactive map.

Inspired by [DeFlock](https://www.deflock.me) and [track-openroaming-passpoint](https://github.com/simeononsecurity/track-openroaming-passpoint).

[![Dependency Graph](https://github.com/simeononsecurity/flock-finder/actions/workflows/dependabot/update-graph/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/dependabot/update-graph) [![pages-build-deployment](https://github.com/simeononsecurity/flock-finder/actions/workflows/pages/pages-build-deployment/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/pages/pages-build-deployment) [![Update Flock Camera Data](https://github.com/simeononsecurity/flock-finder/actions/workflows/update-data.yml/badge.svg)](https://github.com/simeononsecurity/flock-finder/actions/workflows/update-data.yml)

<!-- STATS_START -->
| Metric | Value |
|--------|-------|
| 📸 **Cameras Mapped** | 30,441 |
| 📡 **OUI Prefixes with Data** | 8 / 31 |
| 🌎 **Countries** | 104 |
| 🗺️ **Regions** | 50 |
| 🕐 **Last Updated** | 2026-07-21 |
| 📦 **Data Retention** | 730 days (2 years) |
<!-- STATS_END -->

> *Stats update automatically after each scan via GitHub Actions.*

---

> [!WARNING]
> **Take this map with a grain of salt.** WiGLE is a crowdsourced, passively-collected dataset that is updated sporadically on a per-location basis — it is **not** a live feed. Flock cameras **do not broadcast continuously**; they wake briefly only to upload data, meaning WiGLE records depend entirely on someone happening to be wardriving in the right place at the right time. Locations may be stale, incomplete, or reflect cameras that have since been moved or removed.
>
> **This dashboard is a general awareness tool, not a source of truth.** For accurate, real-time, local detection use the hardware devices by [STSCollective](https://stscollective.com) described below — they implement @NitekryDPaul's actual detection method directly on an ESP32 and can detect Flock cameras as you drive past them.

---

## 🔍 How It Works

Flock Safety ALPR cameras have WiFi transceivers that periodically wake to upload captured license plate data. These transmissions use MAC addresses with identifiable **OUI** (Organizationally Unique Identifier) prefixes.

**@NitekryDPaul** discovered 30 of these OUI prefixes through promiscuous-mode 2.4 GHz analysis. A 31st was contributed by **Michael / DeFlockJoplin** during field testing in Joplin, MO.

This project:
1. Takes those 31 known Flock Safety WiFi OUI prefixes
2. Queries the WiGLE WiFi database for networks matching each prefix
3. Deduplicates and exports results as GeoJSON + CSV
4. Displays camera locations on a dark-themed interactive Leaflet map

> **Note:** WiGLE is a historical, crowdsourced WiFi survey database — it does **not** use @NitekryDPaul's active detection technique. WiGLE entries are submitted by volunteers wardriving with passive scanners, so coverage is uneven and timestamps may be months or years old. The map is best used as a rough geographic reference, not a definitive or current inventory.

### Detection Strategy (from @NitekryDPaul's research)

Flock cameras spend most of their duty cycle **asleep**, waking briefly to upload. The key insight is matching on `addr1` (receiver/destination) in addition to `addr2` (transmitter) — revealing devices that a transmitter-only sniff would miss.

Combined with wildcard probe request detection (802.11 management frames type=0 subtype=4 with empty SSID), this yields a very tight signature: **11 of 12 cameras caught with only 2 false positives** in field testing.

> **This is the gold-standard detection method — and it requires dedicated hardware running in the field.** The WiGLE-based map in this repo does *not* implement addr1 matching; it can only see what WiGLE volunteers have already passively logged. For real-time, on-the-ground detection using this exact technique, see the **[STSCollective FlockYou devices](https://stscollective.com)** — ESP32-based detectors that scan for Flock OUI signatures as you drive, with LED and/or audio alerts the moment a camera is detected.

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
# Full scan — all 31 OUI prefixes, worldwide
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
│   ├── flock_ouis.csv    # 31 known Flock Safety OUI prefixes
│   ├── flock_cameras.geojson  # Output: camera locations (GeoJSON)
│   ├── flock_cameras.csv      # Output: camera locations (CSV)
│   └── scan_stats.json        # Output: scan statistics
├── docs/
│   └── index.html        # Interactive web map (Leaflet + dark theme)
└── .github/
    └── workflows/
        └── update-data.yml  # GitHub Actions: daily auto-update
```

---

## 📡 Flock Safety WiFi OUI Prefixes

31 known prefixes identified by **@NitekryDPaul** + **DeFlockJoplin**:

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

---

## ⚙️ GitHub Actions (Automated Updates)

The included workflow runs daily and auto-commits updated camera data:

1. Add your WiGLE credentials as **repository secrets**:
   - `WIGLE_API_NAME` — your API name from wigle.net/account
   - `WIGLE_API_TOKEN` — your API token

2. The workflow runs at 6 AM UTC daily, or manually via "Run workflow"

3. If new data is found, it commits updated GeoJSON/CSV/stats automatically

---

## 🔒 API Key Security

- The `.env` file containing your WiGLE API credentials is **gitignored** — it will never be committed
- For GitHub Actions, credentials are stored as **repository secrets** (encrypted)
- Never commit API keys to the repository

---

## 🙏 Credits

- **OUI Research:** [@NitekryDPaul](https://github.com/NitekryDPaul) — all 30 original OUI prefixes and the promiscuous-mode detection strategy
- **Field Testing:** [DeFlockJoplin](https://github.com/DeflockJoplin/flock-you) — 31st OUI prefix (`82:6B:F2`) and wildcard probe tightening
- **Inspired by:** [DeFlock](https://www.deflock.me) (ALPR mapping) and [track-openroaming-passpoint](https://github.com/simeononsecurity/track-openroaming-passpoint) (WiGLE data mining)
- **Data Source:** [WiGLE](https://wigle.net) — crowdsourced WiFi/cell network database
- **Map:** [Leaflet](https://leafletjs.com) + [OpenStreetMap](https://www.openstreetmap.org)

---

## ⚖️ Legal & Ethics

This project uses only **publicly available data** from the WiGLE database, which aggregates voluntarily contributed WiFi survey data. No hacking, unauthorized access, or proprietary systems are involved.

The goal is **transparency** — communities have a right to know where surveillance infrastructure is deployed in their neighborhoods.

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.
