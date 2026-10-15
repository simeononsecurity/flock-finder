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
- Every record also carries a `confidence` **evidence tier** (see §8) so
  consumers can tell a Flock-named SSID apart from a bare OUI hit — and tell
  both apart from a record whose own SSID identifies other hardware.

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
`scripts/oui_metadata.py`. Each prefix carries a `tier` (`high` | `mfr`) — see §8.

The generated dataset (`flock_cameras.geojson`, `flock_cameras.csv`, `by_oui/`)
is **published rather than committed** and is served from the site's `/data/`
paths and from the `data-latest` release; see the README section
*"Where Is the Data?"* for the download commands and the reasoning.

## 3. Coordinate precision

Eligible records are published at **full precision, exactly as WiGLE reports
them, and are never modified or truncated.** Accuracy matters for mapping — reducing
precision was found to mislocate points, so the data is passed through
unchanged. The collector only *validates* coordinates (drops missing / out‑of‑
range / null‑island values); it never rounds them. Records whose SSID identifies
unrelated hardware are suppressed before any public point artifact is written.


## 4. Retention

Records whose most recent sighting (`lasttime`) is older than **2 years** are
pruned automatically on each scan.

## 5. Requesting a correction or removal

If you believe a point is wrong, misattributed, or should be removed:

Use the private contact form at <https://simeononsecurity.com/contactus>. Include
an approximate area and a short explanation, such as "not a Flock camera",
"camera removed", or "wrong location". Do not post precise BSSIDs,
coordinates, addresses, or private-location details in public issues. The
maintainer will request any necessary identifier through private follow-up.

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

## 8. Evidence tiers, exclusions and the market flag

An OUI match is weak evidence on its own — OUIs belong to chipset and contract
manufacturers, so unrelated hardware shares the same MAC space. Every published
record therefore carries a `confidence` field, and the headline counts only the
records that survive that classification. Data dictionary entries for these
fields live in [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

| `confidence` | Meaning |
|--------------|---------|
| `ssid_correlated` | The SSID matches an anchored Flock naming pattern (`Flock`, `Flock-XXXXXX`, `Flock Camera net.`, `FS Ext Battery…`). This is correlation, not confirmation. |
| `oui_high` | High-confidence Flock OUI; SSID absent, hidden or unrecognised. Suspected, unverified. |
| `oui_mfr` | Contract-manufacturer OUI (Liteon/USI) that also ships in unrelated products. Weakest evidence. |

`identified_other` is an internal classification. Such records do not appear in
public GeoJSON, CSV, per-prefix point files, or candidate point files.

`identified_other` is produced by a **denylist of SSIDs** that prove the record
is something else — `ClickShare*` (Barco presentation units), `SMARTGATE_*`
(gateway/intercom APs), `DIRECT-*` (consumer TV adapters), `AndroidAP` (phone
hotspots) and `Flock ALPR [*`. That last pattern is *wardriving-tool verdict
text* stored in the SSID field (e.g. `Flock ALPR [wifi_receiver_oui;low]`), not
a broadcast from a camera; it contains the word "flock", so it is denylisted
ahead of the correlation patterns.

Positive SSID matching uses full-string, case-insensitive expressions. The
accepted families are `^flock$`, `^flock[-_]?[0-9a-f]{3,12}$`,
`^flock camera net\.?$`, and `^fs ext battery( <device-id>)?$`. A name which
merely contains the word `flock` does not count.
The SSID-discovery pass still uses API prefix wildcards to gather candidates,
then applies these anchored rules before publishing candidate points.

**Denylisting is regex-based**, so a new spelling of a known non-camera SSID cannot
slip through the way a literal prefix would:

| Rule | What it proves |
|------|----------------|
| `clickshare` | Barco ClickShare presentation units |
| `smartgate_` | SMARTGATE_###### gateway / intercom APs |
| `direct-` | DIRECT-xx consumer set-top / TV adapters |
| `androidap` | Android phone hotspots |
| `audi\s*hud` | Audi head-up-display WiFi (car infotainment) |
| `max[\s-]*printer` | MAX-PRINTER office printers |
| `^\s*flock[\s_-]*alpr\b` | **detector output ingested as data** — see below |

### Detector output ingested as data

30 records carry *another tool's verdict string* in the SSID column — e.g.
`Flock ALPR [wifi_receiver_oui;low]`, `Flock ALPR [wifi_bssid_oui;low]`,
`Flock ALPR [wifi_oui_wildcard_probe;medium]`. All 30 sit in Texas, and the tool
that produced them tagged 26 `low` and 4 `medium`.

These are not observations of a camera. Someone ran a detector, wrote its output
into the SSID column, uploaded the result to WiGLE, and this project's collector
duly queried it — so counting them would mean scoring a detector's *opinion* as
evidence, and they would otherwise inflate the SSID-correlated subset (they
contain the word "flock"). They are excluded outright: no Flock camera broadcasts
an SSID beginning `Flock ALPR`, so the rule can be as blunt as it is here.

Aggregate denylist counts remain in `scan_stats.json`. Precise points do not.
The collector stores a SHA-256 fingerprint of each suppressed BSSID in
`data/suppression_registry.json`. The registry contains no coordinates. It
prevents a later blank or renamed SSID from restoring the point during an
incremental merge.

**Prefixes are not equally credible.** The README breakdown measures, per prefix,
how many of its records carry a Flock SSID and how many name other hardware. Nine
prefixes with ≥200 records are *neither correlated nor contradicted* — including
the single largest contributor, `E0:4F:43` (22,303 records, 66 SSID-correlated) —
because their SSIDs are absent or unreadable. An OUI match on those means little
on its own, and the published table says so rather than implying that all 39
prefixes are equally good fingerprints.

**Market flag.** Records outside `PRIMARY_MARKET_COUNTRIES` (currently `US`) carry
`"out_of_market": true`. They are flagged rather than deleted, because Flock has
expanded internationally and hiding evidence would be worse than labelling it.
Roughly half of the OUI-matched records fall outside the US, which is itself a
signal that many of them are unrelated hardware rather than an argument for
deleting them.

Neither the tiers nor the market flag change what a record *is*: every record
remains **suspected**, and the tiers describe the strength of the evidence, not a
confirmation from Flock that a device is theirs.

## 9. Release completeness and authoritative rebuilds

The collector records `complete`, `interrupted`, or `failed` for every OUI
pagination attempt. A release is written only after every requested OUI reports
`complete`. Interrupted pages retain local resume records and cursors. Failed or
incomplete scans never advance the `last_completed` checkpoint.

`data/rebuild_policy.json` defines the authoritative rebuild interval, release
size floor, and labeled-fixture quality floors. `--rebuild-if-due` selects a
resumable authoritative rebuild when the interval elapsed. Before then, the
same scheduled command continues the normal incremental scan.

`data/validation/ssid_labels.csv` is the committed human-reviewed fixture set.
`data/validation/ssid_metrics.json` reports precision, recall, sample size, and
review date for each OUI represented in the fixture. Validation rejects drift
between the labels and the report.
