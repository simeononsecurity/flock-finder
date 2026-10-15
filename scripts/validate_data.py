#!/usr/bin/env python3
"""
Flock Finder — Data Integrity Validator
=======================================
Validates the committed public data artifacts. Intended to run in CI so a bad
commit can't publish malformed data.

Checks:
  * data/flock_cameras.geojson is valid GeoJSON with Point features
  * every feature has valid, in-range coordinates (FULL precision is kept —
    coordinates are never truncated so the map stays accurate)
  * data/flock_ouis.csv contains only well-formed OUIs
  * data/flock_ouis.json (if present) matches the CSV

Exit code 0 = OK, 1 = one or more problems found.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from oui_metadata import load_oui_metadata  # noqa: E402
from validation import (  # noqa: E402
    CONFIDENCE_IDENTIFIED_OTHER,
    evaluate_labeled_samples,
    is_valid_latlon,
    is_valid_oui,
    netid_fingerprint,
    ssid_denylist_match,
    validate_suppression_registry,
)

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"


def validate_geojson(path: Path, errors: list, suppression_registry: dict = None) -> None:
    if not path.exists():
        print(f"  [i] {path.name} not present — skipping")
        return
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        errors.append(f"{path.name}: invalid JSON ({exc})")
        return

    if data.get("type") != "FeatureCollection":
        errors.append(f"{path.name}: top-level type is not FeatureCollection")

    features = data.get("features", [])
    suppression_enforced = bool((data.get("properties") or {}).get("suppression_applied"))
    if suppression_enforced and (data.get("properties") or {}).get("release_status") != "complete":
        errors.append(f"{path.name}: suppression-aware release is not marked complete")
    bad_coords = 0
    exposed_suppressed = 0
    registry_entries = (suppression_registry or {}).get("entries", {})
    for feat in features:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        if not is_valid_latlon(lat, lon):
            bad_coords += 1
        props = feat.get("properties") or {}
        netid = props.get("netid") or ""
        registry_match = False
        try:
            registry_match = netid_fingerprint(netid) in registry_entries
        except ValueError:
            pass
        if registry_match or (
            suppression_enforced
            and (
                props.get("confidence") == CONFIDENCE_IDENTIFIED_OTHER
                or ssid_denylist_match(props.get("ssid") or "")
            )
        ):
            exposed_suppressed += 1

    if bad_coords:
        errors.append(f"{path.name}: {bad_coords} feature(s) with invalid coordinates")
    if exposed_suppressed:
        errors.append(f"{path.name}: {exposed_suppressed} suppressed point(s) exposed")
    if not suppression_enforced:
        print(f"  [i] {path.name}: legacy release without suppression metadata")
    print(f"  [✓] {path.name}: {len(features)} features checked ({bad_coords} bad coords)")


def validate_public_csv(
    path: Path,
    errors: list,
    suppression_registry: dict,
    suppression_enforced: bool,
) -> None:
    if not path.exists():
        return
    exposed = 0
    rows = 0
    registry_entries = suppression_registry.get("entries", {})
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            registry_match = False
            try:
                registry_match = netid_fingerprint(row.get("netid") or "") in registry_entries
            except ValueError:
                pass
            if registry_match or (
                suppression_enforced
                and (
                    row.get("confidence") == CONFIDENCE_IDENTIFIED_OTHER
                    or ssid_denylist_match(row.get("ssid") or "")
                )
            ):
                exposed += 1
    if exposed:
        errors.append(f"{path.name}: {exposed} suppressed CSV row(s) exposed")
    print(f"  [✓] {path.name}: {rows} CSV row(s) checked")


def load_and_validate_suppression_registry(errors: list) -> dict:
    path = DATA_DIR / "suppression_registry.json"
    if not path.exists():
        errors.append("suppression_registry.json is missing")
        return {}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"suppression_registry.json: invalid JSON ({exc})")
        return {}
    registry_errors = validate_suppression_registry(registry)
    errors.extend(f"suppression_registry.json: {error}" for error in registry_errors)
    print(f"  [✓] suppression_registry.json: {len(registry.get('entries', {}))} entries checked")
    return registry


def validate_labeled_fixtures(errors: list) -> dict:
    fixture_path = DATA_DIR / "validation" / "ssid_labels.csv"
    report_path = DATA_DIR / "validation" / "ssid_metrics.json"
    policy_path = DATA_DIR / "rebuild_policy.json"
    if not fixture_path.exists():
        errors.append("validation/ssid_labels.csv is missing")
        return {}
    try:
        with fixture_path.open(newline="", encoding="utf-8") as handle:
            report = evaluate_labeled_samples(csv.DictReader(handle))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        committed_report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"validation fixtures: {exc}")
        return {}

    precision_floor = policy.get("minimum_fixture_precision", 1.0)
    recall_floor = policy.get("minimum_fixture_recall", 1.0)
    for oui, metrics in report.items():
        precision = metrics["precision"]
        recall = metrics["recall"]
        if precision is None:
            errors.append(f"{oui}: fixture precision is undefined")
        elif precision < precision_floor:
            errors.append(f"{oui}: fixture precision {precision:.3f} below {precision_floor:.3f}")
        if recall is None:
            errors.append(f"{oui}: fixture recall is undefined")
        elif recall < recall_floor:
            errors.append(f"{oui}: fixture recall {recall:.3f} below {recall_floor:.3f}")
        precision_text = "n/a" if precision is None else f"{precision:.3f}"
        recall_text = "n/a" if recall is None else f"{recall:.3f}"
        print(
            f"  [✓] {oui}: precision={precision_text}, recall={recall_text}, "
            f"samples={metrics['sample_size']}, reviewed={metrics['review_date']}"
        )
    if committed_report.get("classifier") != "ssid_correlated":
        errors.append("validation/ssid_metrics.json: wrong classifier")
    if committed_report.get("per_prefix") != report:
        errors.append("validation/ssid_metrics.json: report is out of sync with labels")
    return report


def validate_ouis(errors: list) -> None:
    csv_path = DATA_DIR / "flock_ouis.csv"
    if not csv_path.exists():
        errors.append("flock_ouis.csv is missing")
        return
    entries = load_oui_metadata(csv_path)
    for e in entries:
        if not is_valid_oui(e["oui"]):
            errors.append(f"flock_ouis.csv: malformed OUI {e['oui']!r}")
    ouis = [e["oui"] for e in entries]
    if len(ouis) != len(set(ouis)):
        errors.append("flock_ouis.csv: duplicate OUI prefixes present")
    print(f"  [✓] flock_ouis.csv: {len(entries)} OUI entries checked")

    json_path = DATA_DIR / "flock_ouis.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text())
            json_ouis = [o["oui"] for o in payload.get("ouis", [])]
            if set(json_ouis) != set(ouis):
                errors.append("flock_ouis.json is out of sync with flock_ouis.csv "
                              "(run scripts/oui_metadata.py)")
            else:
                print(f"  [✓] flock_ouis.json: in sync ({len(json_ouis)} entries)")
        except Exception as exc:
            errors.append(f"flock_ouis.json: invalid JSON ({exc})")


def validate_stats(errors: list) -> None:
    """
    Check that scan_stats.json's counts are internally consistent.

    The dataset itself (flock_cameras.geojson / .csv / by_oui/) is published rather
    than committed, so in PR CI this file is the only artifact that still carries
    dataset-wide numbers — which makes it the right place to catch a headline that
    no longer adds up to the records behind it.
    """
    path = DATA_DIR / "scan_stats.json"
    if not path.exists():
        print("  [i] scan_stats.json not present — skipping")
        return
    try:
        stats = json.loads(path.read_text())
    except Exception as exc:
        errors.append(f"scan_stats.json: invalid JSON ({exc})")
        return

    total = stats.get("total_cameras")
    by_conf = stats.get("by_confidence")
    if not isinstance(total, int) or not isinstance(by_conf, dict):
        print("  [i] scan_stats.json: no confidence breakdown yet — skipping")
        return

    if sum(by_conf.values()) != total:
        errors.append(
            f"scan_stats.json: by_confidence sums to {sum(by_conf.values()):,} but "
            f"total_cameras is {total:,}"
        )

    if stats.get("suppression_applied"):
        if stats.get("release_status") != "complete":
            errors.append("scan_stats.json: suppression-aware release is not marked complete")
        if "ssid_confirmed" in by_conf:
            errors.append("scan_stats.json: legacy ssid_confirmed key in suppression-aware release")
        correlated = by_conf.get("ssid_correlated", 0)
        oui_only = by_conf.get("oui_high", 0) + by_conf.get("oui_mfr", 0)
        if stats.get("ssid_correlated") != correlated:
            errors.append("scan_stats.json: ssid_correlated total does not match by_confidence")
        if stats.get("oui_only") != oui_only:
            errors.append("scan_stats.json: oui_only total does not match by_confidence")
        if by_conf.get(CONFIDENCE_IDENTIFIED_OTHER, 0):
            errors.append("scan_stats.json: suppressed records remain in public confidence counts")
        if not isinstance(stats.get("suppression"), dict):
            errors.append("scan_stats.json: suppression aggregate is missing")

    actionable = stats.get("total_actionable")
    other = by_conf.get("identified_other", 0)
    if actionable is not None and actionable != total - other:
        errors.append(
            f"scan_stats.json: total_actionable {actionable:,} != total_cameras - "
            f"identified_other ({total - other:,})"
        )
    print(
        f"  [✓] scan_stats.json: {total:,} records, {actionable:,} actionable "
        f"(breakdown adds up)"
        if actionable is not None else
        f"  [✓] scan_stats.json: {total:,} records (breakdown adds up)"
    )


def main() -> int:
    print("Validating Flock Finder data artifacts…")
    errors: list = []
    registry = load_and_validate_suppression_registry(errors)
    validate_labeled_fixtures(errors)
    try:
        stats_payload = json.loads((DATA_DIR / "scan_stats.json").read_text())
    except (OSError, ValueError):
        stats_payload = {}
    suppression_enforced = bool(stats_payload.get("suppression_applied"))
    candidate_geojson = DATA_DIR / "ssid_candidate_cameras.geojson"
    try:
        candidate_payload = json.loads(candidate_geojson.read_text())
    except (OSError, ValueError):
        candidate_payload = {}
    candidate_suppression = bool(
        (candidate_payload.get("properties") or {}).get("suppression_applied")
    )
    validate_geojson(DATA_DIR / "flock_cameras.geojson", errors, registry)
    validate_geojson(candidate_geojson, errors, registry)
    validate_public_csv(
        DATA_DIR / "flock_cameras.csv", errors, registry, suppression_enforced
    )
    validate_public_csv(
        DATA_DIR / "ssid_candidate_cameras.csv", errors, registry, candidate_suppression
    )
    for sub in sorted((DATA_DIR / "by_oui").glob("*.geojson")):
        validate_geojson(sub, errors, registry)
    for sub in sorted((DATA_DIR / "by_oui").glob("*.csv")):
        companion = sub.with_suffix(".geojson")
        try:
            companion_payload = json.loads(companion.read_text())
        except (OSError, ValueError):
            companion_payload = {}
        companion_suppression = bool(
            (companion_payload.get("properties") or {}).get("suppression_applied")
        )
        validate_public_csv(sub, errors, registry, companion_suppression)
    validate_ouis(errors)
    validate_stats(errors)

    if errors:
        print("\n[✗] Data validation FAILED:")
        for e in errors:
            print(f"    - {e}")
        return 1
    print("\n[✓] All data artifacts passed validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
