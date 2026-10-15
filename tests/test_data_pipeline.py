"""Fail-closed collector, suppression, retry, and classifier regressions."""

import csv
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from ssid_query import (  # noqa: E402
    query_ssid_pattern,
    summarize_dataset_coverage,
    suppress_candidate_networks,
)
from validate_data import validate_geojson, validate_public_csv  # noqa: E402
from validation import (  # noqa: E402
    CONFIDENCE_SSID_CORRELATED,
    empty_suppression_registry,
    evaluate_labeled_samples,
    netid_fingerprint,
    ssid_correlates_flock,
    suppress_records,
    validate_release_candidate,
)
from wigle_query import (  # noqa: E402
    PAGE_SIZE,
    PaginationStatus,
    authoritative_rebuild_due,
    load_existing_data,
    load_ouis,
    load_scan_state,
    merge_networks,
    query_oui,
    retry_delay,
    select_authoritative_rebuild,
    write_stats,
)


def _record(netid="70:C9:4E:12:34:56", ssid="Flock", lasttime="2026-10-14"):
    return {
        "netid": netid,
        "ssid": ssid,
        "trilat": 39.1,
        "trilong": -94.5,
        "lasttime": lasttime,
        "oui_match": netid[:8],
        "confidence": CONFIDENCE_SSID_CORRELATED,
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)

    def search_wifi(self, **_kwargs):
        return next(self.responses)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, ValueError):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


def _api_record(index):
    return {
        "netid": f"70:C9:4E:00:{index // 256:02X}:{index % 256:02X}",
        "ssid": "Flock",
        "trilat": 39.1,
        "trilong": -94.5,
    }


def test_query_outcomes_are_explicit_and_complete_only_at_terminal_page():
    result = query_oui(
        FakeClient([{"success": True, "results": [_api_record(1)], "totalResults": 1}]),
        "70:C9:4E",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.COMPLETE
    assert len(result.records) == 1
    assert result.search_after is None


def test_rate_limit_returns_interrupted_resume_cursor_without_completion():
    first_page = [_api_record(i) for i in range(PAGE_SIZE)]
    result = query_oui(
        FakeClient([
            {"success": True, "results": first_page, "searchAfter": "cursor-1"},
            {"success": False, "error": "rate_limited"},
        ]),
        "70:C9:4E",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.INTERRUPTED
    assert result.search_after == "cursor-1"
    assert result.page == 1
    assert len(result.records) == PAGE_SIZE


def test_transient_failure_uses_bounded_backoff_then_fails():
    sleeps = []
    result = query_oui(
        FakeClient([{"success": False, "error": "timeout"}] * 3),
        "70:C9:4E",
        max_attempts=3,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )
    assert result.status == PaginationStatus.FAILED
    assert sleeps == [1.0, 2.0]
    assert retry_delay(20, random_value=1.0) == 30.0


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"success": False, "error": "unauthorized"}, "unauthorized"),
        ({"success": True, "results": "malformed"}, "invalid_results"),
        ({"success": False, "error": "http_503", "transient": True}, "http_503"),
    ],
)
def test_oui_query_never_completes_authorization_malformed_or_server_failures(
    response, error
):
    result = query_oui(
        FakeClient([response] * 3),
        "70:C9:4E",
        max_attempts=3,
        sleep_fn=lambda _delay: None,
        random_fn=lambda: 0.5,
    )
    assert result.status == PaginationStatus.FAILED
    assert result.error == error


def test_full_page_without_cursor_fails_closed():
    result = query_oui(
        FakeClient([{"success": True, "results": [_api_record(i) for i in range(PAGE_SIZE)]}]),
        "70:C9:4E",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.FAILED
    assert result.error == "missing_search_after_on_full_page"


def test_ssid_query_returns_complete_only_at_terminal_page():
    result = query_ssid_pattern(
        FakeSession([
            FakeResponse(200, {
                "success": True,
                "results": [_api_record(1)],
                "totalResults": 1,
            })
        ]),
        "Flock%",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.COMPLETE
    assert len(result.records) == 1
    assert result.search_after is None


def test_ssid_query_rate_limit_returns_resume_state():
    first_page = [_api_record(i) for i in range(PAGE_SIZE)]
    result = query_ssid_pattern(
        FakeSession([
            FakeResponse(200, {
                "success": True,
                "results": first_page,
                "searchAfter": "ssid-cursor-1",
            }),
            FakeResponse(429),
        ]),
        "Flock%",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.INTERRUPTED
    assert result.search_after == "ssid-cursor-1"
    assert result.page == 1
    assert len(result.records) == PAGE_SIZE


def test_ssid_query_retries_server_errors_then_fails():
    sleeps = []
    result = query_ssid_pattern(
        FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(503)]),
        "Flock%",
        max_attempts=3,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )
    assert result.status == PaginationStatus.FAILED
    assert result.error == "http_503"
    assert sleeps == [1.0, 2.0]


def test_ssid_query_authorization_failure_never_completes():
    result = query_ssid_pattern(
        FakeSession([FakeResponse(401)]),
        "Flock%",
        sleep_fn=lambda _delay: None,
    )
    assert result.status == PaginationStatus.FAILED
    assert result.error == "unauthorized"


def test_ssid_query_handles_empty_404_and_rejects_invalid_json():
    empty = query_ssid_pattern(
        FakeSession([FakeResponse(404)]),
        "Flock%",
        sleep_fn=lambda _delay: None,
    )
    invalid_json = query_ssid_pattern(
        FakeSession([FakeResponse(200, ValueError("invalid"))]),
        "Flock%",
        sleep_fn=lambda _delay: None,
    )
    assert empty.status == PaginationStatus.COMPLETE
    assert empty.records == []
    assert invalid_json.status == PaginationStatus.FAILED
    assert invalid_json.error == "invalid_json"


def test_release_validation_rejects_interrupted_failed_and_shrunken_candidates():
    records = {"70:C9:4E:12:34:56": _record()}
    complete = validate_release_candidate(records, ["70:C9:4E"], {"70:C9:4E": "complete"})
    assert complete.valid

    for status in ("interrupted", "failed"):
        result = validate_release_candidate(
            records, ["70:C9:4E"], {"70:C9:4E": status}
        )
        assert not result.valid

    shrunk = validate_release_candidate(
        records,
        ["70:C9:4E"],
        {"70:C9:4E": "complete"},
        baseline_count=10,
        minimum_record_ratio=0.9,
    )
    assert not shrunk.valid


def test_existing_release_loader_fails_closed_on_malformed_geojson(tmp_path):
    path = tmp_path / "flock_cameras.geojson"
    path.write_text('{"type": "FeatureCollection"}', encoding="utf-8")
    with pytest.raises(ValueError, match="FeatureCollection"):
        load_existing_data(path)


def test_resume_state_loader_fails_closed_on_corruption(tmp_path):
    path = tmp_path / "scan_state.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="scan state"):
        load_scan_state(path)


def test_suppression_registry_is_hashed_and_blocks_renamed_incremental_record():
    netid = "74:4C:A1:12:34:56"
    unrelated = _record(netid=netid, ssid="ClickShare-Boardroom")
    public, registry, summary = suppress_records(
        {netid: unrelated}, empty_suppression_registry(), "2026-10-14T00:00:00+00:00"
    )
    assert public == {}
    assert netid not in json.dumps(registry)
    assert netid_fingerprint(netid) in registry["entries"]
    assert summary["by_reason"] == {"clickshare": 1}

    renamed = _record(netid=netid, ssid="")
    merged = merge_networks({}, [renamed], registry)
    assert merged == {}
    public, _registry, summary = suppress_records(
        {netid: renamed}, registry, "2026-10-15T00:00:00+00:00"
    )
    assert public == {}
    assert summary["by_reason"] == {"clickshare": 1}


def test_suppression_aware_artifact_validation_rejects_precise_unrelated_points(tmp_path):
    netid = "74:4C:A1:12:34:56"
    geojson = tmp_path / "points.geojson"
    geojson.write_text(json.dumps({
        "type": "FeatureCollection",
        "properties": {"suppression_applied": True},
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-94.5, 39.1]},
            "properties": {"netid": netid, "ssid": "ClickShare-Boardroom"},
        }],
    }), encoding="utf-8")
    errors = []
    validate_geojson(geojson, errors, empty_suppression_registry())
    assert any("suppressed point" in error for error in errors)

    csv_path = tmp_path / "points.csv"
    csv_path.write_text(
        "netid,ssid,confidence\n"
        f"{netid},ClickShare-Boardroom,identified_other\n",
        encoding="utf-8",
    )
    errors = []
    validate_public_csv(csv_path, errors, empty_suppression_registry(), True)
    assert any("suppressed CSV" in error for error in errors)


def test_registry_suppression_is_enforced_for_legacy_csv(tmp_path):
    netid = "74:4C:A1:12:34:56"
    _public, registry, _summary = suppress_records(
        {netid: _record(netid=netid, ssid="ClickShare-Room")},
        empty_suppression_registry(),
        "2026-10-14T00:00:00+00:00",
    )
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "netid,ssid,confidence\n"
        f"{netid},,oui_mfr\n",
        encoding="utf-8",
    )
    errors = []
    validate_public_csv(csv_path, errors, registry, False)
    assert any("suppressed CSV" in error for error in errors)


def test_candidate_pipeline_applies_shared_registry_before_publication():
    known = "74:4C:A1:12:34:56"
    unrelated = _record(netid=known, ssid="ClickShare-Room")
    _public, registry, _summary = suppress_records(
        {known: unrelated}, empty_suppression_registry(), "2026-10-14T00:00:00+00:00"
    )
    candidates = [
        _record(netid=known, ssid=""),
        _record(netid="70:C9:4E:12:34:57", ssid="Flock-7EBB9D"),
        _record(netid="70:C9:4E:12:34:58", ssid="Flock ALPR [x;low]"),
        _record(netid="70:C9:4E:12:34:59", ssid="FlockSupport"),
    ]
    public, updated, summary = suppress_candidate_networks(
        candidates, registry, "2026-10-15T00:00:00+00:00"
    )
    assert [record["netid"] for record in public] == ["70:C9:4E:12:34:57"]
    assert summary["excluded_from_release"] == 2
    assert summary["by_reason"] == {"clickshare": 1, "detector_verdict": 1}
    assert summary["unmatched_ssid_records"] == 1
    assert updated["entry_count"] == 2

def test_anchored_patterns_reject_broad_flock_substrings():
    for ssid in ("Flock", "Flock-7EBB9D", "Flock001", "FS Ext Battery 1234"):
        assert ssid_correlates_flock(ssid)
    for ssid in ("Bird Flock Guest", "Flock of Birds", "FlockSupport", "unflocked"):
        assert not ssid_correlates_flock(ssid)


def test_committed_fixture_reports_per_prefix_quality_and_review_date():
    validation_dir = os.path.join(os.path.dirname(__file__), "..", "data", "validation")
    path = os.path.join(validation_dir, "ssid_labels.csv")
    with open(path, newline="", encoding="utf-8") as handle:
        report = evaluate_labeled_samples(csv.DictReader(handle))
    assert report
    for metrics in report.values():
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 1.0
        assert metrics["sample_size"] >= 2
        assert metrics["review_date"] == "2026-10-14"
    with open(os.path.join(validation_dir, "ssid_metrics.json"), encoding="utf-8") as handle:
        committed = json.load(handle)
    assert committed["classifier"] == "ssid_correlated"
    assert committed["per_prefix"] == report


def test_authoritative_rebuild_due_uses_configured_interval():
    policy = {"authoritative_rebuild_interval_days": 30}
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert authoritative_rebuild_due({}, policy, now)
    assert authoritative_rebuild_due(
        {"last_authoritative_rebuild": "2026-09-01T00:00:00+00:00"}, policy, now
    )
    assert not authoritative_rebuild_due(
        {"last_authoritative_rebuild": "2026-10-01T00:00:00+00:00"}, policy, now
    )


def test_scheduled_mode_selects_rebuild_when_due_and_incremental_when_not_due():
    policy = {"authoritative_rebuild_interval_days": 30}
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert select_authoritative_rebuild(
        False,
        True,
        {"last_authoritative_rebuild": "2026-09-01T00:00:00+00:00"},
        policy,
        now,
    )
    assert not select_authoritative_rebuild(
        False,
        True,
        {"last_authoritative_rebuild": "2026-10-01T00:00:00+00:00"},
        policy,
        now,
    )
    assert select_authoritative_rebuild(True, False, {}, policy, now)


def test_incremental_stats_preserve_last_authoritative_rebuild(tmp_path):
    path = tmp_path / "scan_stats.json"
    completed = "2026-10-01T00:00:00+00:00"
    path.write_text(json.dumps({"last_authoritative_rebuild": completed}), encoding="utf-8")
    write_stats(
        {},
        39,
        0,
        0,
        path,
        classifier_validation={"classifier": "ssid_correlated", "per_prefix": {}},
    )
    assert json.loads(path.read_text())["last_authoritative_rebuild"] == completed


def test_oui_query_list_rejects_malformed_or_duplicate_rows(tmp_path):
    malformed = tmp_path / "malformed.csv"
    malformed.write_text("oui\n70:C9:4E\nnot-an-oui\n", encoding="utf-8")
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text("oui\n70:C9:4E\n70:c9:4e\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed OUI"):
        load_ouis(malformed)
    with pytest.raises(ValueError, match="duplicate OUI"):
        load_ouis(duplicate)


def test_coverage_uses_anchored_camera_family_patterns(tmp_path):
    path = tmp_path / "flock_cameras.csv"
    path.write_text(
        "netid,ssid\n"
        "70:C9:4E:00:00:01,Flock Camera net.\n"
        "70:C9:4E:00:00:02,Guest Flock Camera network\n"
        "04:0D:84:00:00:03,FS Ext Battery 1234\n"
        "04:0D:84:00:00:04,My FS Ext Battery Backup\n",
        encoding="utf-8",
    )
    coverage = summarize_dataset_coverage(path)
    assert coverage["flock_camera_net_records"] == 1
    assert coverage["fs_ext_battery_records"] == 1
