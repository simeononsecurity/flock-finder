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
| OUI prefixes | Promiscuous‑mode research by **@NitekryDPaul**; 31st prefix from **DeFlockJoplin**; Flock Safety's own IEEE registration (`B4:1E:52`) and the FS Ext Battery series from **dougborg/PR#39** |
| Sightings | [WiGLE](https://wigle.net) — a crowdsourced, volunteer‑wardriven WiFi survey database |
| Geocoding (search only) | [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org) |

WiGLE data is **historical and passively collected**. Coverage is uneven and
timestamps may be months or years old. It is not a live feed.

The canonical OUI list lives in [`data/flock_ouis.csv`](../data/flock_ouis.csv)
and is mirrored to `data/flock_ouis.json` for the web frontend by
`scripts/oui_metadata.py`.

## 3. Coordinate precision

Coordinates are published at **full precision, exactly as WiGLE reports them,
and are never modified or truncated.** Accuracy matters for mapping — reducing
precision was found to mislocate points, so the data is passed through
unchanged. The collector only *validates* coordinates (drops missing / out‑of‑
range / null‑island values); it never rounds them.


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

## 7. Signatures we deliberately do NOT query (and why)

Not every Flock-related radio signature belongs in an OUI query list. Some are
too generic to query, and some are too specific to be a *prefix*. Both cases are
documented here so they don't get re-added by accident.

| Signature | Why it is not a query prefix |
|-----------|------------------------------|
| `00:03:7F` — Qualcomm Atheros **QCA9377** chipset OUI | This is the WiFi radio *chipset* used in Flock's MSM8953-generation cameras, but the OUI itself is a generic Atheros assignment shared with a huge installed base of unrelated hardware. Queried alone it would return mostly non-Flock devices, so it is only ever a **low-confidence signal that requires corroboration** (an SSID, an exact default MAC, or a BLE match). It is intentionally **not** in `data/flock_ouis.csv`. |
| `00:03:7F:50:00:01` / `00:03:7F:4F:00:16` — factory-default QCA9377 radio MACs (from `bdwlan30.bin` / `otp30.bin`) | These are *exact 6-byte addresses*, not prefixes: what an unprovisioned camera transmits before Flock's provisioning step rewrites the MAC. They are far more specific than the bare OUI above, but (a) this project's collector searches by 3-octet prefix and (b) a provisioned camera no longer uses them. The on-device detector in [flock-you-esp32](https://github.com/simeononsecurity/flock-you-esp32) scores an exact match on these addresses as high confidence. |

Two related notes:

- Several prefixes in this list (`F4:6A:DD`, `F8:A2:D6` — Liteon; `00:F4:8D`,
  `D0:39:57`, `E8:D0:FC` — USI) belong to **contract manufacturers**, not to
  Flock directly. They are queried because they have been observed on confirmed
  Flock hardware in the field, but they are expected to carry a higher
  false-positive rate than a Flock-registered prefix; the firmware project above
  scores them as a separate, lower-confidence tier for that reason.
- `FS Ext Battery%` is the one **SSID** pattern beyond `Flock%`/`FLOCK%` that the
  discovery pass queries (see `scripts/ssid_query.py`). It exists because the
  battery-pack/accessory series advertises that SSID and is invisible to
  `Flock-*` matching — the same blind spot that hid `Flock Camera net.` from
  earlier SSID searches. SSID matches never enter `flock_ouis.csv` on their own;
  they only surface **candidate** prefixes awaiting field verification (see the
  README's *SSID-Discovery: Candidate OUI Prefixes* section).
