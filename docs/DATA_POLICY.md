# Flock Finder — Data Policy, Provenance & Corrections

This document explains **what the data means, where it comes from, how precise
it is, and how to request corrections.** Please read it before relying on
anything this project publishes.

## 1. Everything here is *suspected*, not confirmed

Every record is a **suspected** Flock Safety device inferred from a single
signal: the WiFi **OUI** (the first three octets of a MAC address) matching a
prefix associated with Flock hardware.

An OUI match is a heuristic, **not** proof:

- OUIs are assigned to manufacturers/chipset vendors, not to a single product.
  A matching prefix can belong to unrelated hardware.
- MAC addresses can be randomized, reassigned, or spoofed.
- Cameras get moved or decommissioned; stale records persist.

For this reason:

- Every GeoJSON feature carries `"match_confidence": "suspected"`.
- The combined dataset's top-level properties include
  `"match_confidence": "suspected"`.
- The map popups say **"Suspected Flock Camera — Unconfirmed."**

Do not treat this as an authoritative inventory or as evidence about any
specific address or person.

## 2. Provenance (where the data comes from)

| Layer | Source |
|-------|--------|
| OUI prefixes | Promiscuous‑mode research by **@NitekryDPaul**; 31st prefix from **DeFlockJoplin** |
| Sightings | [WiGLE](https://wigle.net) — a crowdsourced, volunteer‑wardriven WiFi survey database |
| Geocoding (search only) | [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org) |

WiGLE data is **historical and passively collected**. Coverage is uneven and
timestamps may be months or years old. It is not a live feed.

The canonical OUI list lives in [`data/flock_ouis.csv`](../data/flock_ouis.csv)
and is mirrored to `data/flock_ouis.json` for the web frontend by
`scripts/oui_metadata.py`.

## 3. Coordinate precision (why points are approximate)

Published coordinates are deliberately **truncated to 3 decimal places
(~110 m)**. The goal is to communicate a *neighborhood‑level area*, not a
precise, surveyable point. WiGLE's trilateration is already approximate; we
reduce precision further on purpose.

This is enforced centrally in `scripts/wigle_query.py` (see
`redact_coordinates` / `PUBLIC_COORD_PRECISION`) and verified in CI by
`scripts/validate_data.py`.

## 4. Retention

Records whose most recent sighting (`lasttime`) is older than **2 years** are
pruned automatically on each scan.

## 5. Requesting a correction or removal

If you believe a point is wrong, misattributed, or should be removed:

1. Open a GitHub issue titled `Data correction: <area / BSSID prefix>`.
2. Include the approximate location and, if known, the BSSID prefix.
3. Explain what's incorrect (e.g., "not a Flock camera", "camera removed",
   "wrong location").

Please **do not** post full precise addresses of private individuals. Because
the dataset is regenerated from WiGLE on a schedule, corrections may need to be
encoded as OUI/data adjustments to persist across runs — maintainers will
advise in the issue.

## 6. Ethics & scope

This project uses only **publicly available** WiGLE data. The intent is
transparency about **surveillance infrastructure**, not surveillance of people.
Please use it accordingly.
