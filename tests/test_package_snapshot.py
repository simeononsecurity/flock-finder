import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "package_snapshot.py"
UPDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update-data.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-pages.yml"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def write_fixture(data_dir: Path) -> None:
    data_dir.mkdir()
    (data_dir / "by_oui").mkdir()
    rows = (
        "netid,ssid,firsttime,lasttime\n"
        "00:11:22:33:44:55,Flock,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z\n"
        "00:11:22:33:44:56,Flock,2026-01-02T00:00:00Z,2026-02-02T00:00:00Z\n"
    )
    (data_dir / "flock_cameras.csv").write_text(rows, encoding="utf-8")
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-87.0, 41.0]},
            "properties": {
                "netid": f"00:11:22:33:44:5{index}",
                "firsttime": f"2026-01-0{index + 1}T00:00:00Z",
                "lasttime": f"2026-02-0{index + 1}T00:00:00Z",
            },
        }
        for index in range(2)
    ]
    geojson = {"type": "FeatureCollection", "features": features, "properties": {}}
    encoded_geojson = json.dumps(geojson) + "\n"
    (data_dir / "flock_cameras.geojson").write_text(encoded_geojson, encoding="utf-8")
    (data_dir / "by_oui" / "00_11_22.geojson").write_text(encoded_geojson, encoding="utf-8")
    (data_dir / "scan_stats.json").write_text(
        json.dumps(
            {
                "release_schema_version": 2,
                "release_status": "complete",
                "suppression_applied": True,
                "scan_timestamp": "2026-02-03T04:05:06+00:00",
                "total_cameras": 2,
                "by_confidence": {
                    "ssid_correlated": 2,
                    "oui_high": 0,
                    "oui_mfr": 0,
                    "identified_other": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (data_dir / "scan_state.json").write_text(
        json.dumps(
            {
                "ouis": {
                    "00:11:22": {"since_date": "20260101"},
                    "00:11:23": {"since_date": "20260115"},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    asset_dir = data_dir.parent / "docs" / "assets"
    (asset_dir / "bssid").mkdir(parents=True)
    (asset_dir / "bssid" / "00.json").write_text(
        json.dumps({"001122334455": [-87.0, 41.0], "001122334456": [-87.1, 41.1]}),
        encoding="utf-8",
    )
    header = bytearray(127)
    header[:7] = b"PMTiles"
    header[7] = 3
    struct.pack_into("<Q", header, 72, 12)
    struct.pack_into("<Q", header, 80, 7)
    struct.pack_into("<Q", header, 88, 5)
    (asset_dir / "flock-cameras.pmtiles").write_bytes(header)
    (asset_dir / "build-map-assets.mjs").write_text("// Snapshot test fixture.\n", encoding="utf-8")


def run_build(data_dir: Path, output_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "build",
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(output_dir),
            "--source-commit",
            SOURCE_COMMIT,
            "--scan-started-at",
            "2026-02-03T04:00:00Z",
            "--base-status",
            "explicit_full_scan",
            "--repository",
            "example/flock-finder",
            "--extra-artifact-root",
            str(data_dir.parent / "docs" / "assets"),
            "--require-map-assets",
            "--tippecanoe-version",
            "tippecanoe v2.79.0",
            "--tippecanoe-source-commit",
            "68ab8dcc229f95b8b25877697d5e8d66783af503",
            "--map-asset-builder",
            str(data_dir.parent / "docs" / "assets" / "build-map-assets.mjs"),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=data_dir.parent,
    )


def sole_file(directory: Path, suffix: str) -> Path:
    matches = list(directory.glob(f"*{suffix}"))
    assert len(matches) == 1
    return matches[0]


def test_build_is_deterministic_and_records_provenance(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"

    first = run_build(data_dir, first_output)
    second = run_build(data_dir, second_output)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_archive = sole_file(first_output, ".tar.gz")
    second_archive = sole_file(second_output, ".tar.gz")
    assert (
        hashlib.sha256(first_archive.read_bytes()).digest()
        == hashlib.sha256(second_archive.read_bytes()).digest()
    )

    manifest = json.loads(sole_file(first_output, ".manifest.json").read_text())
    assert manifest["manifest_schema_version"] == 1
    assert manifest["source"]["commit"] == SOURCE_COMMIT
    assert manifest["generators"]["tippecanoe"] == {
        "source_commit": "68ab8dcc229f95b8b25877697d5e8d66783af503",
        "source_repository": "https://github.com/felt/tippecanoe",
        "version": "tippecanoe v2.79.0",
    }
    assert manifest["generators"]["map_asset_builder"]["path"] == (
        "docs/assets/build-map-assets.mjs"
    )
    assert manifest["record_counts"]["canonical"] == 2
    assert manifest["record_counts"]["per_oui_geojson_total"] == 2
    assert manifest["record_counts"]["frontend_bssid_shard_total"] == 2
    assert manifest["release_gates"] == {
        "base_dataset_status": "explicit_full_scan",
        "release_schema_version": 2,
        "release_status": "complete",
        "suppression_applied": True,
        "confidence_tier_counts": {
            "ssid_correlated": 2,
            "oui_high": 0,
            "oui_mfr": 0,
            "identified_other": 0,
        },
    }
    assert manifest["scan_window"] == {
        "completed_at": "2026-02-03T04:05:06Z",
        "earliest_since_date": "2026-01-01",
        "first_observation_at": "2026-01-01T00:00:00Z",
        "last_observation_at": "2026-02-02T00:00:00Z",
        "latest_since_date": "2026-01-15",
        "started_at": "2026-02-03T04:00:00Z",
    }
    pmtiles = next(entry for entry in manifest["files"] if entry["path"].endswith(".pmtiles"))
    assert pmtiles["record_count"] == 7
    assert pmtiles["record_count_kind"] == "tile_entries"
    assert pmtiles["schema"]["addressed_tiles"] == 12


def test_workflow_restores_and_gates_base_before_any_release_upload() -> None:
    workflow = UPDATE_WORKFLOW.read_text(encoding="utf-8")
    restore = workflow.index("Restore verified base snapshot or require explicit full scan")
    package = workflow.index("Build immutable snapshot package")
    upload = workflow.index("Publish immutable snapshot release")
    assert restore < package < upload
    assert "BASE_DATASET_STATUS=verified_restored_base" in workflow
    assert "BASE_DATASET_STATUS=explicit_full_scan" in workflow
    assert '--base-status "$BASE_DATASET_STATUS"' in workflow


def test_pages_deploy_waits_for_a_verified_snapshot_update() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    triggers = workflow.partition("permissions:")[0]

    assert "  push:" not in triggers
    assert "  workflow_run:" in triggers
    assert "Update Flock Camera Data" in triggers
    assert "No verified immutable snapshot pointer exists" in workflow


def test_verify_detects_changed_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    output_dir = tmp_path / "snapshot"
    build = run_build(data_dir, output_dir)
    assert build.returncode == 0, build.stderr
    manifest = sole_file(output_dir, ".manifest.json")

    verified = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--manifest",
            str(manifest),
            "--data-dir",
            str(data_dir),
            "--paths",
            "flock_cameras.csv",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr

    with (data_dir / "flock_cameras.csv").open("a", encoding="utf-8") as handle:
        handle.write("changed\n")
    rejected = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            "--manifest",
            str(manifest),
            "--data-dir",
            str(data_dir),
            "--paths",
            "flock_cameras.csv",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "size mismatch" in rejected.stderr


def test_build_rejects_canonical_count_mismatch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    stats_path = data_dir / "scan_stats.json"
    stats = json.loads(stats_path.read_text())
    stats["total_cameras"] = 3
    stats["by_confidence"]["ssid_correlated"] = 3
    stats_path.write_text(json.dumps(stats) + "\n", encoding="utf-8")

    result = run_build(data_dir, tmp_path / "snapshot")

    assert result.returncode != 0
    assert "canonical record counts disagree" in result.stderr


def test_build_rejects_incomplete_release_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    stats_path = data_dir / "scan_stats.json"
    stats = json.loads(stats_path.read_text())
    stats["release_status"] = "interrupted"
    stats_path.write_text(json.dumps(stats) + "\n", encoding="utf-8")

    result = run_build(data_dir, tmp_path / "snapshot")

    assert result.returncode != 0
    assert "do not describe a complete release" in result.stderr


def test_build_rejects_unrelated_public_tier_count(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    stats_path = data_dir / "scan_stats.json"
    stats = json.loads(stats_path.read_text())
    stats["by_confidence"]["ssid_correlated"] = 1
    stats["by_confidence"]["identified_other"] = 1
    stats_path.write_text(json.dumps(stats) + "\n", encoding="utf-8")

    result = run_build(data_dir, tmp_path / "snapshot")

    assert result.returncode != 0
    assert "identified unrelated-device records" in result.stderr


def test_restore_verifies_pointer_archive_manifest_and_map_assets(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    output_dir = tmp_path / "snapshot"
    build = run_build(data_dir, output_dir)
    assert build.returncode == 0, build.stderr

    restored = tmp_path / "restored"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            "--latest",
            str(output_dir / "latest.json"),
            "--archive",
            str(sole_file(output_dir, ".tar.gz")),
            "--manifest",
            str(sole_file(output_dir, ".manifest.json")),
            "--output-dir",
            str(restored),
            "--require",
            "data/flock_cameras.geojson",
            "--require",
            "data/flock_cameras.csv",
            "--require",
            "docs/assets/flock-cameras.pmtiles",
            "--require-glob",
            "docs/assets/bssid/*.json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (restored / "data" / "flock_cameras.geojson").is_file()
    assert (restored / "docs" / "assets" / "flock-cameras.pmtiles").is_file()
    assert (restored / "docs" / "assets" / "bssid" / "00.json").is_file()


def test_restore_rejects_latest_pointer_hash_mismatch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    output_dir = tmp_path / "snapshot"
    build = run_build(data_dir, output_dir)
    assert build.returncode == 0, build.stderr
    latest_path = output_dir / "latest.json"
    latest = json.loads(latest_path.read_text())
    latest["archive"]["sha256"] = "0" * 64
    latest_path.write_text(json.dumps(latest), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            "--latest",
            str(latest_path),
            "--archive",
            str(sole_file(output_dir, ".tar.gz")),
            "--manifest",
            str(sole_file(output_dir, ".manifest.json")),
            "--output-dir",
            str(tmp_path / "restored"),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "archive SHA-256 does not match latest pointer" in result.stderr


def test_restore_rejects_corrupt_base_archive(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    write_fixture(data_dir)
    output_dir = tmp_path / "snapshot"
    build = run_build(data_dir, output_dir)
    assert build.returncode == 0, build.stderr
    archive = sole_file(output_dir, ".tar.gz")
    archive.write_bytes(archive.read_bytes() + b"corrupt")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            "--latest",
            str(output_dir / "latest.json"),
            "--archive",
            str(archive),
            "--manifest",
            str(sole_file(output_dir, ".manifest.json")),
            "--output-dir",
            str(tmp_path / "restored"),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "archive size does not match latest pointer" in result.stderr
