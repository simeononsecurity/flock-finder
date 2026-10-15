#!/usr/bin/env python3
"""Build and verify uniquely tagged, hash-verified Flock Finder snapshots."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

MANIFEST_SCHEMA_VERSION = 1
LATEST_POINTER_SCHEMA_VERSION = 1
EXCLUDED_DATA_FILES = {"scan_state.json", "ssid_scan_state.json"}
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
RELEASE_SCHEMA_VERSION = 2
BASE_DATASET_STATUSES = {"verified_restored_base", "explicit_full_scan"}

MEDIA_TYPES = {
    ".csv": "text/csv",
    ".geojson": "application/geo+json",
    ".json": "application/json",
    ".mbtiles": "application/vnd.mapbox-vector-tile",
    ".mvt": "application/vnd.mapbox-vector-tile",
    ".pbf": "application/x-protobuf",
    ".pmtiles": "application/vnd.pmtiles",
}


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_timestamp(value: str, field: str) -> str:
    """Validate an ISO 8601 timestamp and return an explicit UTC form."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot_id_for(completed_at: str, source_commit: str) -> str:
    parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    stamp = parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{source_commit[:12].lower()}"


def update_window(window: dict[str, str | None], first: Any, last: Any) -> None:
    for key, value, comparison in (
        ("first_observation_at", first, min),
        ("last_observation_at", last, max),
    ):
        if not isinstance(value, str) or not value:
            continue
        current = window[key]
        window[key] = value if current is None else comparison(current, value)


def csv_metadata(path: Path, observation_window: dict[str, str | None]) -> tuple[int, dict]:
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        for row in reader:
            count += 1
            update_window(observation_window, row.get("firsttime"), row.get("lasttime"))
    return count, {"format": "CSV", "columns": columns}


def geojson_metadata(path: Path, observation_window: dict[str, str | None]) -> tuple[int, dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid GeoJSON file {path}: {exc}") from exc
    if document.get("type") != "FeatureCollection" or not isinstance(
        document.get("features"), list
    ):
        raise ValueError(f"GeoJSON snapshot input is not a FeatureCollection: {path}")
    geometry_types: set[str] = set()
    property_fields: set[str] = set()
    for feature in document["features"]:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if isinstance(geometry, dict) and isinstance(geometry.get("type"), str):
            geometry_types.add(geometry["type"])
        properties = feature.get("properties")
        if isinstance(properties, dict):
            property_fields.update(str(key) for key in properties)
            update_window(
                observation_window, properties.get("firsttime"), properties.get("lasttime")
            )
    return len(document["features"]), {
        "format": "GeoJSON",
        "root_type": "FeatureCollection",
        "geometry_types": sorted(geometry_types),
        "property_fields": sorted(property_fields),
        "collection_property_fields": sorted((document.get("properties") or {}).keys()),
    }


def json_metadata(path: Path) -> tuple[int | None, dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc
    count: int | None = None
    if isinstance(document, list):
        count = len(document)
        schema = {"format": "JSON", "top_level_type": "array"}
    elif isinstance(document, dict):
        schema = {"format": "JSON", "top_level_type": "object"}
        if path.parent.name == "bssid":
            invalid = [
                key
                for key, value in document.items()
                if not re.fullmatch(r"[0-9A-Fa-f]{12}", str(key))
                or not isinstance(value, list)
                or len(value) != 2
                or not all(isinstance(coordinate, (int, float)) for coordinate in value)
            ]
            if invalid:
                raise ValueError(f"invalid BSSID lookup record in {path}: {invalid[0]}")
            count = len(document)
            schema.update(
                {
                    "key_pattern": "12 hexadecimal BSSID characters",
                    "value_model": "[longitude, latitude] numeric pair",
                }
            )
        else:
            schema["top_level_keys"] = sorted(document)
            for key in ("total_cameras", "total_candidates", "record_count"):
                value = document.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    count = value
                    schema["record_count_field"] = key
                    break
    else:
        schema = {"format": "JSON", "top_level_type": type(document).__name__}
    return count, schema


def pmtiles_metadata(path: Path) -> tuple[int, dict]:
    with path.open("rb") as handle:
        header = handle.read(127)
    if len(header) < 96 or header[:7] != b"PMTiles":
        raise ValueError(f"invalid PMTiles header: {path}")
    version = header[7]
    addressed_tiles = struct.unpack_from("<Q", header, 72)[0]
    tile_entries = struct.unpack_from("<Q", header, 80)[0]
    tile_contents = struct.unpack_from("<Q", header, 88)[0]
    return tile_entries, {
        "format": "PMTiles",
        "spec_version": version,
        "addressed_tiles": addressed_tiles,
        "tile_entries": tile_entries,
        "tile_contents": tile_contents,
    }


def mbtiles_metadata(path: Path) -> tuple[int, dict]:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])
            columns = [row[1] for row in connection.execute("PRAGMA table_info(tiles)")]
            metadata = dict(connection.execute("SELECT name, value FROM metadata"))
    except sqlite3.Error as exc:
        raise ValueError(f"invalid MBTiles database {path}: {exc}") from exc
    return count, {
        "format": "MBTiles",
        "tile_columns": columns,
        "declared_format": metadata.get("format"),
        "minzoom": metadata.get("minzoom"),
        "maxzoom": metadata.get("maxzoom"),
    }


def artifact_metadata(
    path: Path, observation_window: dict[str, str | None]
) -> tuple[int | None, dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return csv_metadata(path, observation_window)
    if suffix == ".geojson":
        return geojson_metadata(path, observation_window)
    if suffix == ".json":
        return json_metadata(path)
    if suffix == ".pmtiles":
        return pmtiles_metadata(path)
    if suffix == ".mbtiles":
        return mbtiles_metadata(path)
    if suffix in {".mvt", ".pbf"}:
        return None, {"format": "Mapbox Vector Tile", "container": suffix.removeprefix(".")}
    return None, {"format": suffix.removeprefix(".").upper() or "binary"}


def record_count_kind(path: Path, schema: dict, record_count: int | None) -> str | None:
    if record_count is None:
        return None
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "rows"
    if suffix == ".geojson":
        return "features"
    if suffix == ".pmtiles":
        return "tile_entries"
    if suffix == ".mbtiles":
        return "tiles"
    if path.parent.name == "bssid":
        return "bssid_records"
    if suffix == ".json" and schema.get("record_count_field"):
        return f"declared:{schema['record_count_field']}"
    return "items"


def discover_data_files(data_dir: Path, output_dir: Path) -> list[Path]:
    files = []
    output_resolved = output_dir.resolve()
    for path in data_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"snapshot input must not contain symlinks: {path}")
        if not path.is_file() or path.name in EXCLUDED_DATA_FILES:
            continue
        try:
            path.resolve().relative_to(output_resolved)
        except ValueError:
            files.append(path)
    if not files:
        raise ValueError(f"no snapshot inputs found under {data_dir}")
    return sorted(files, key=lambda item: item.relative_to(data_dir).as_posix())


def discover_extra_artifacts(
    roots: list[Path], output_dir: Path, repository_root: Path
) -> list[tuple[str, Path]]:
    """Find generated data and tile artifacts outside the canonical data directory."""
    discovered: list[tuple[str, Path]] = []
    output_resolved = output_dir.resolve()
    for root in roots:
        if not root.exists():
            continue
        if not root.is_dir():
            raise ValueError(f"extra artifact root is not a directory: {root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"snapshot input must not contain symlinks: {path}")
            if not path.is_file() or path.suffix.lower() not in MEDIA_TYPES:
                continue
            try:
                path.resolve().relative_to(output_resolved)
                continue
            except ValueError:
                pass
            try:
                relative = path.resolve().relative_to(repository_root).as_posix()
            except ValueError as exc:
                raise ValueError(
                    f"extra artifacts must be inside the repository root {repository_root}: {path}"
                ) from exc
            discovered.append((relative, path))
    return sorted(discovered)


def query_window_from_state(path: Path | None) -> dict[str, str | None]:
    result = {"earliest_since_date": None, "latest_since_date": None}
    if path is None or not path.exists():
        return result
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid scan state {path}: {exc}") from exc
    states = document.get("ouis", {})
    dates = sorted(
        value["since_date"]
        for value in states.values()
        if isinstance(value, dict)
        and isinstance(value.get("since_date"), str)
        and re.fullmatch(r"\d{8}", value["since_date"])
    )
    if dates:
        result["earliest_since_date"] = f"{dates[0][:4]}-{dates[0][4:6]}-{dates[0][6:]}"
        result["latest_since_date"] = f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}"
    return result


def add_bytes_to_tar(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def add_file_to_tar(archive: tarfile.TarFile, name: str, path: Path) -> None:
    info = tarfile.TarInfo(name)
    info.size = path.stat().st_size
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    with path.open("rb") as handle:
        archive.addfile(info, handle)


def build_archive(
    archive_path: Path, files: Iterable[tuple[str, Path]], manifest_bytes: bytes
) -> None:
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                add_bytes_to_tar(archive, "manifest.json", manifest_bytes)
                for relative, path in files:
                    add_file_to_tar(archive, relative, path)


def write_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"GitHub output {key} contains a newline")
            handle.write(f"{key}={value}\n")


def build_snapshot(args: argparse.Namespace) -> int:
    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    source_commit = args.source_commit.lower()
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise ValueError("source commit must be a full 40-64 character hexadecimal commit ID")
    stats_path = data_dir / "scan_stats.json"
    if not stats_path.exists():
        raise ValueError(f"required scan statistics are missing: {stats_path}")
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if stats.get("release_schema_version") != RELEASE_SCHEMA_VERSION:
        raise ValueError(
            f"scan statistics require release_schema_version {RELEASE_SCHEMA_VERSION}"
        )
    if stats.get("release_status") != "complete":
        raise ValueError("scan statistics do not describe a complete release")
    if stats.get("suppression_applied") is not True:
        raise ValueError("scan statistics do not confirm suppression was applied")
    if args.base_status not in BASE_DATASET_STATUSES:
        raise ValueError("release requires a verified restored base or explicit full scan")
    confidence_counts = stats.get("by_confidence")
    required_confidence_tiers = ("ssid_correlated", "oui_high", "oui_mfr", "identified_other")
    if not isinstance(confidence_counts, dict) or any(
        not isinstance(confidence_counts.get(key), int)
        or isinstance(confidence_counts.get(key), bool)
        or confidence_counts[key] < 0
        for key in required_confidence_tiers
    ):
        raise ValueError("scan statistics contain invalid confidence-tier counts")
    if confidence_counts["identified_other"] != 0:
        raise ValueError("public release still contains identified unrelated-device records")
    public_confidence_count = sum(confidence_counts[key] for key in required_confidence_tiers)
    if public_confidence_count != stats.get("total_cameras"):
        raise ValueError("confidence-tier counts do not equal total_cameras")
    completed_at = normalize_timestamp(stats.get("scan_timestamp"), "scan_timestamp")
    started_at = normalize_timestamp(args.scan_started_at, "scan_started_at")
    if started_at > completed_at:
        raise ValueError("scan_started_at must not be after scan_timestamp")

    snapshot_id = snapshot_id_for(completed_at, source_commit)
    release_tag = f"data-snapshot-{snapshot_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    repository_root = Path.cwd().resolve()
    data_files = discover_data_files(data_dir, output_dir)
    snapshot_inputs = [
        (f"data/{path.relative_to(data_dir).as_posix()}", path) for path in data_files
    ]
    snapshot_inputs.extend(
        discover_extra_artifacts(args.extra_artifact_root, output_dir, repository_root)
    )
    input_names = [name for name, _ in snapshot_inputs]
    if len(input_names) != len(set(input_names)):
        raise ValueError("snapshot inputs contain duplicate archive paths")
    pmtiles_paths = sorted(name for name in input_names if name.endswith(".pmtiles"))
    bssid_paths = sorted(
        name
        for name in input_names
        if name.startswith("docs/assets/bssid/") and name.endswith(".json")
    )
    if args.require_map_assets:
        if "docs/assets/flock-cameras.pmtiles" not in pmtiles_paths:
            raise ValueError("required map asset is missing: docs/assets/flock-cameras.pmtiles")
        if not bssid_paths:
            raise ValueError("required BSSID map shards are missing: docs/assets/bssid/*.json")
    if bool(args.tippecanoe_version) != bool(args.tippecanoe_source_commit):
        raise ValueError("Tippecanoe version and source commit must be provided together")
    if pmtiles_paths and (not args.tippecanoe_version or not args.tippecanoe_source_commit):
        raise ValueError(
            "PMTiles snapshots require --tippecanoe-version and --tippecanoe-source-commit"
        )
    if args.tippecanoe_source_commit and not SOURCE_COMMIT_RE.fullmatch(
        args.tippecanoe_source_commit
    ):
        raise ValueError("Tippecanoe source commit must be a full hexadecimal commit ID")
    observation_window: dict[str, str | None] = {
        "first_observation_at": None,
        "last_observation_at": None,
    }
    file_entries = []
    archive_inputs = []
    counts_by_path: dict[str, int] = {}

    for relative, path in sorted(snapshot_inputs):
        record_count, schema = artifact_metadata(path, observation_window)
        schema_hash = sha256_bytes(canonical_json_bytes(schema))
        entry = {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "media_type": MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            "record_count": record_count,
            "record_count_kind": record_count_kind(path, schema, record_count),
            "schema": schema,
            "schema_sha256": schema_hash,
        }
        file_entries.append(entry)
        archive_inputs.append((relative, path))
        if record_count is not None:
            counts_by_path[relative] = record_count

    csv_count = counts_by_path.get("data/flock_cameras.csv")
    geojson_count = counts_by_path.get("data/flock_cameras.geojson")
    stats_count = stats.get("total_cameras")
    expected_counts = [
        count for count in (csv_count, geojson_count, stats_count) if count is not None
    ]
    if len(set(expected_counts)) > 1:
        raise ValueError(
            "canonical record counts disagree: "
            f"CSV={csv_count}, GeoJSON={geojson_count}, scan_stats={stats_count}"
        )
    canonical_count = expected_counts[0] if expected_counts else None
    by_oui_counts = [
        count
        for path, count in counts_by_path.items()
        if path.startswith("data/by_oui/") and path.endswith(".geojson")
    ]
    if by_oui_counts and canonical_count is not None and sum(by_oui_counts) != canonical_count:
        raise ValueError(
            "per-OUI GeoJSON record counts do not equal the canonical count: "
            f"{sum(by_oui_counts)} != {canonical_count}"
        )
    frontend_bssid_counts = [
        count
        for path, count in counts_by_path.items()
        if "/bssid/" in path and path.endswith(".json")
    ]
    if frontend_bssid_counts and canonical_count is not None:
        frontend_total = sum(frontend_bssid_counts)
        if frontend_total != canonical_count:
            raise ValueError(
                "frontend BSSID shard record counts do not equal the canonical count: "
                f"{frontend_total} != {canonical_count}"
            )

    scan_state = args.scan_state or data_dir / "scan_state.json"
    query_window = query_window_from_state(scan_state)
    generators: dict[str, dict[str, Any]] = {}
    if args.tippecanoe_version:
        generators["tippecanoe"] = {
            "version": args.tippecanoe_version,
            "source_commit": args.tippecanoe_source_commit.lower(),
            "source_repository": "https://github.com/felt/tippecanoe",
        }
    if args.map_asset_builder:
        builder = args.map_asset_builder.resolve()
        if not builder.is_file():
            raise ValueError(f"map asset builder is missing: {builder}")
        try:
            builder_path = builder.relative_to(repository_root).as_posix()
        except ValueError as exc:
            raise ValueError("map asset builder must be inside the repository") from exc
        generators["map_asset_builder"] = {
            "path": builder_path,
            "sha256": sha256_file(builder),
        }

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "release_tag": release_tag,
        "created_at": completed_at,
        "source": {
            "repository": args.repository,
            "commit": source_commit,
            "data_provider": "WiGLE",
            "data_provider_url": "https://wigle.net/",
            "data_rights_file": "DATA_LICENSE",
        },
        "scan_window": {
            "started_at": started_at,
            "completed_at": completed_at,
            **query_window,
            **observation_window,
        },
        "record_counts": {
            "canonical": canonical_count,
            "flock_cameras_csv": csv_count,
            "flock_cameras_geojson": geojson_count,
            "per_oui_geojson_total": sum(by_oui_counts) if by_oui_counts else None,
            "frontend_bssid_shard_total": (
                sum(frontend_bssid_counts) if frontend_bssid_counts else None
            ),
        },
        "release_gates": {
            "base_dataset_status": args.base_status,
            "release_schema_version": stats["release_schema_version"],
            "release_status": stats["release_status"],
            "suppression_applied": stats["suppression_applied"],
            "confidence_tier_counts": {
                key: confidence_counts[key] for key in required_confidence_tiers
            },
        },
        "generators": generators,
        "files": file_entries,
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    stem = f"flock-finder-snapshot-{snapshot_id}"
    manifest_path = output_dir / f"{stem}.manifest.json"
    archive_path = output_dir / f"{stem}.tar.gz"
    checksums_path = output_dir / f"{stem}.sha256"
    latest_path = output_dir / "latest.json"
    manifest_path.write_bytes(manifest_bytes)
    build_archive(archive_path, archive_inputs, manifest_bytes)

    manifest_hash = sha256_file(manifest_path)
    archive_hash = sha256_file(archive_path)
    checksums = f"{archive_hash}  {archive_path.name}\n" f"{manifest_hash}  {manifest_path.name}\n"
    checksums_path.write_text(checksums, encoding="utf-8")
    release_url = (
        f"https://github.com/{args.repository}/releases/tag/{release_tag}"
        if args.repository
        else None
    )
    latest = {
        "latest_pointer_schema_version": LATEST_POINTER_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "release_tag": release_tag,
        "release_url": release_url,
        "created_at": completed_at,
        "source_commit": source_commit,
        "canonical_record_count": canonical_count,
        "archive": {
            "name": archive_path.name,
            "sha256": archive_hash,
            "bytes": archive_path.stat().st_size,
        },
        "manifest": {
            "name": manifest_path.name,
            "sha256": manifest_hash,
            "bytes": manifest_path.stat().st_size,
        },
        "checksums": {
            "name": checksums_path.name,
            "sha256": sha256_file(checksums_path),
            "bytes": checksums_path.stat().st_size,
        },
    }
    latest_path.write_text(json.dumps(latest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    outputs = {
        "snapshot_id": snapshot_id,
        "release_tag": release_tag,
        "archive": str(archive_path),
        "manifest": str(manifest_path),
        "checksums": str(checksums_path),
        "latest_pointer": str(latest_path),
    }
    if args.github_output:
        write_github_outputs(args.github_output, outputs)
    print(json.dumps(outputs, indent=2, sort_keys=True))
    return 0


def requested_manifest_paths(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    normalized = set()
    for value in values:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe manifest path requested: {value}")
        normalized.add(path.as_posix() if path.parts[0] == "data" else f"data/{path.as_posix()}")
    return normalized


def verify_snapshot(args: argparse.Namespace) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot manifest schema version")
    selected = requested_manifest_paths(args.paths)
    entries = {
        entry["path"]: entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    if selected is not None:
        missing_entries = sorted(selected - entries.keys())
        if missing_entries:
            raise ValueError(f"paths missing from manifest: {', '.join(missing_entries)}")
        entries = {key: entries[key] for key in sorted(selected)}
    if not entries:
        raise ValueError("manifest contains no files to verify")

    data_dir = args.data_dir.resolve()
    errors = []
    for relative, entry in sorted(entries.items()):
        manifest_path = PurePosixPath(relative)
        if (
            manifest_path.is_absolute()
            or ".." in manifest_path.parts
            or manifest_path.parts[0] != "data"
        ):
            errors.append(f"unsafe manifest path: {relative}")
            continue
        local_path = data_dir.joinpath(*manifest_path.parts[1:])
        if not local_path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        if local_path.stat().st_size != entry.get("bytes"):
            errors.append(f"size mismatch: {relative}")
            continue
        if sha256_file(local_path) != entry.get("sha256"):
            errors.append(f"SHA-256 mismatch: {relative}")
    if errors:
        raise ValueError("snapshot verification failed: " + "; ".join(errors))
    print(f"Verified {len(entries)} snapshot file(s) against {args.manifest}")
    return 0


def manifest_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot manifest schema version")
    entries: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("manifest contains an invalid file entry")
        relative = entry["path"]
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"unsafe manifest path: {relative}")
        if relative in entries:
            raise ValueError(f"duplicate manifest path: {relative}")
        entries[relative] = entry
    if not entries:
        raise ValueError("manifest contains no files")
    return entries


def verify_restored_files(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    errors = []
    for relative, entry in sorted(entries.items()):
        local_path = root.joinpath(*PurePosixPath(relative).parts)
        if not local_path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        if local_path.stat().st_size != entry.get("bytes"):
            errors.append(f"size mismatch: {relative}")
            continue
        if sha256_file(local_path) != entry.get("sha256"):
            errors.append(f"SHA-256 mismatch: {relative}")
    if errors:
        raise ValueError("restored snapshot verification failed: " + "; ".join(errors))


def restore_snapshot(args: argparse.Namespace) -> int:
    latest = json.loads(args.latest.read_text(encoding="utf-8"))
    if latest.get("latest_pointer_schema_version") != LATEST_POINTER_SCHEMA_VERSION:
        raise ValueError("unsupported latest pointer schema version")

    archive_descriptor = latest.get("archive")
    manifest_descriptor = latest.get("manifest")
    if not isinstance(archive_descriptor, dict) or not isinstance(manifest_descriptor, dict):
        raise ValueError("latest pointer is missing archive or manifest metadata")
    for path, descriptor, label in (
        (args.archive, archive_descriptor, "archive"),
        (args.manifest, manifest_descriptor, "manifest"),
    ):
        if descriptor.get("name") != path.name:
            raise ValueError(f"{label} filename does not match latest pointer")
        if descriptor.get("bytes") != path.stat().st_size:
            raise ValueError(f"{label} size does not match latest pointer")
        if descriptor.get("sha256") != sha256_file(path):
            raise ValueError(f"{label} SHA-256 does not match latest pointer")

    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest_entries(manifest)
    for pointer_key, manifest_key in (
        ("snapshot_id", "snapshot_id"),
        ("release_tag", "release_tag"),
        ("source_commit", "source"),
    ):
        manifest_value = (
            manifest.get(manifest_key, {}).get("commit")
            if manifest_key == "source"
            else manifest.get(manifest_key)
        )
        if latest.get(pointer_key) != manifest_value:
            raise ValueError(f"latest pointer {pointer_key} does not match manifest")

    required = set(args.require)
    missing_required = sorted(required - entries.keys())
    if missing_required:
        raise ValueError(f"required paths missing from manifest: {', '.join(missing_required)}")
    for pattern in args.require_glob:
        if not any(fnmatch.fnmatchcase(path, pattern) for path in entries):
            raise ValueError(f"required path pattern missing from manifest: {pattern}")

    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"restore output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    expected_names = {"manifest.json", *entries}
    with tarfile.open(args.archive, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("snapshot archive contains duplicate paths")
        if set(names) != expected_names:
            missing = sorted(expected_names - set(names))
            unexpected = sorted(set(names) - expected_names)
            raise ValueError(
                "snapshot archive contents do not match manifest: "
                f"missing={missing}, unexpected={unexpected}"
            )
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or not member.isreg():
                raise ValueError(f"unsafe snapshot archive member: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read snapshot archive member: {member.name}")
            if member.name == "manifest.json":
                if source.read() != manifest_bytes:
                    raise ValueError("embedded manifest does not match released manifest")
                continue
            destination = output_dir.joinpath(*member_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as target:
                shutil.copyfileobj(source, target)

    verify_restored_files(output_dir, entries)
    print(f"Restored and verified {len(entries)} snapshot file(s) into {output_dir}")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a deterministic snapshot package")
    build.add_argument("--data-dir", type=Path, default=Path("data"))
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--source-commit", required=True)
    build.add_argument("--scan-started-at", required=True)
    build.add_argument(
        "--base-status",
        required=True,
        choices=sorted(BASE_DATASET_STATUSES),
        help="verified base restoration or an explicitly requested full scan",
    )
    build.add_argument("--scan-state", type=Path)
    build.add_argument(
        "--extra-artifact-root",
        action="append",
        type=Path,
        default=[],
        help="repository directory containing generated JSON, CSV, GeoJSON, or tile artifacts",
    )
    build.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    build.add_argument("--github-output", type=Path)
    build.add_argument("--require-map-assets", action="store_true")
    build.add_argument("--tippecanoe-version")
    build.add_argument("--tippecanoe-source-commit")
    build.add_argument("--map-asset-builder", type=Path)
    build.set_defaults(handler=build_snapshot)

    verify = subparsers.add_parser("verify", help="verify files against a snapshot manifest")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--data-dir", type=Path, default=Path("data"))
    verify.add_argument("--paths", nargs="*")
    verify.set_defaults(handler=verify_snapshot)

    restore = subparsers.add_parser(
        "restore", help="restore one snapshot after verifying its latest pointer and manifest"
    )
    restore.add_argument("--latest", type=Path, required=True)
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--output-dir", type=Path, required=True)
    restore.add_argument("--require", action="append", default=[])
    restore.add_argument("--require-glob", action="append", default=[])
    restore.set_defaults(handler=restore_snapshot)
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
