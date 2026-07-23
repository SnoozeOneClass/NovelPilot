from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config

from app.db.revisions import HEAD_REVISION
from app.db.schema import EXPECTED_TABLE_NAMES

BACKUP_FORMAT_VERSION = 1


class DatabaseHealthError(RuntimeError):
    """A database cannot safely be opened or restored."""


class DatabaseNotQuiescentError(RuntimeError):
    """A backup was requested while execution state was not at a safe boundary."""


class BackupManifestError(RuntimeError):
    """A backup manifest is absent, malformed, or does not bind to its database."""


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    schema_revision: str
    highest_event_sequence: int
    blob_count: int


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    database_filename: str
    schema_revision: str
    highest_event_sequence: int
    blob_count: int
    file_size: int
    sha256: str
    created_at_ms: int

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                asdict(self),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )


def manifest_path_for(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.manifest.json")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _connect_existing(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise DatabaseHealthError(f"Database does not exist: {database_path}")
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _canonical_blob_bytes(row: sqlite3.Row) -> bytes:
    payload = bytes(cast(bytes, row["payload"]))
    stored_size = cast(int, row["stored_size"])
    canonical_size = cast(int, row["canonical_size"])
    if len(payload) != stored_size:
        raise DatabaseHealthError(
            f"Blob stored size mismatch: {row['project_id']}/{row['sha256']}"
        )
    compression = cast(str, row["compression"])
    if compression == "identity-v1":
        canonical = payload
    elif compression == "gzip-v1":
        try:
            canonical = gzip.decompress(payload)
        except (EOFError, OSError) as exc:
            raise DatabaseHealthError(
                f"Blob gzip corruption: {row['project_id']}/{row['sha256']}"
            ) from exc
    else:
        raise DatabaseHealthError(f"Unknown Blob compression: {compression!r}")
    if len(canonical) != canonical_size:
        raise DatabaseHealthError(
            f"Blob canonical size mismatch: {row['project_id']}/{row['sha256']}"
        )
    if hashlib.sha256(canonical).hexdigest() != row["sha256"]:
        raise DatabaseHealthError(f"Blob hash mismatch: {row['project_id']}/{row['sha256']}")
    return canonical


def validate_database(
    database_path: Path,
    *,
    expected_revision: str = HEAD_REVISION,
) -> DatabaseHealth:
    """Validate a closed/snapshot database without mutating it."""
    with closing(_connect_existing(database_path.resolve())) as connection:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity_rows] != ["ok"]:
            raise DatabaseHealthError(f"SQLite integrity_check failed: {integrity_rows!r}")

        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise DatabaseHealthError(f"SQLite foreign_key_check failed: {foreign_key_rows!r}")

        tables = {
            cast(str, row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required_tables = EXPECTED_TABLE_NAMES | {"alembic_version"}
        missing = required_tables - tables
        if missing:
            raise DatabaseHealthError(f"Database is missing tables: {sorted(missing)!r}")

        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        revision = None if revision_row is None else cast(str, revision_row[0])
        if revision != expected_revision:
            raise DatabaseHealthError(
                f"Schema revision mismatch: expected {expected_revision}, found {revision}."
            )

        blob_rows = connection.execute(
            "SELECT project_id, sha256, compression, canonical_size, stored_size, payload "
            "FROM content_blobs ORDER BY project_id, sha256"
        ).fetchall()
        for row in blob_rows:
            _canonical_blob_bytes(row)

        orphan_count = cast(
            int,
            connection.execute(
                "SELECT count(*) FROM content_blobs AS b "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM content_refs AS r "
                "WHERE r.project_id = b.project_id AND r.blob_sha256 = b.sha256)"
            ).fetchone()[0],
        )
        if orphan_count:
            raise DatabaseHealthError(f"Database contains {orphan_count} unreferenced Blobs.")

        event_sequence = cast(
            int,
            connection.execute(
                "SELECT coalesce(max(sequence), 0) FROM domain_events"
            ).fetchone()[0],
        )
        return DatabaseHealth(
            schema_revision=revision,
            highest_event_sequence=event_sequence,
            blob_count=len(blob_rows),
        )


def _assert_quiescent(connection: sqlite3.Connection) -> None:
    running_attempts = cast(
        int,
        connection.execute(
            "SELECT count(*) FROM agent_task_attempts WHERE status = 'running'"
        ).fetchone()[0],
    )
    claimed_slots = cast(
        int,
        connection.execute(
            "SELECT count(*) FROM engine_slot WHERE active_run_id IS NOT NULL"
        ).fetchone()[0],
    )
    active_runs = cast(
        int,
        connection.execute(
            "SELECT count(*) FROM generation_runs "
            "WHERE status IN ('running', 'pause_requested')"
        ).fetchone()[0],
    )
    if running_attempts or claimed_slots or active_runs:
        raise DatabaseNotQuiescentError(
            "Backup requires no running attempts, claimed engine slot, or active run."
        )


def _atomic_write_new(path: Path, payload: bytes) -> None:
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.staging")
    try:
        with staging.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def create_consistent_backup(source_path: Path, destination_path: Path) -> BackupManifest:
    """Create an Online Backup API snapshot; caller must stop/pause the service first."""
    source = source_path.resolve()
    destination = destination_path.resolve()
    manifest_path = manifest_path_for(destination)
    if destination.exists() or manifest_path.exists():
        raise FileExistsError("Backup database and manifest paths must both be new.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")
    try:
        with closing(_connect_existing(source)) as source_connection:
            _assert_quiescent(source_connection)
            with closing(sqlite3.connect(staging)) as destination_connection:
                source_connection.backup(destination_connection)
        health = validate_database(staging)
        file_size = staging.stat().st_size
        digest = _sha256_file(staging)
        os.replace(staging, destination)
        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            database_filename=destination.name,
            schema_revision=health.schema_revision,
            highest_event_sequence=health.highest_event_sequence,
            blob_count=health.blob_count,
            file_size=file_size,
            sha256=digest,
            created_at_ms=time.time_ns() // 1_000_000,
        )
        _atomic_write_new(manifest_path, manifest.to_bytes())
        return manifest
    finally:
        staging.unlink(missing_ok=True)


def read_backup_manifest(backup_path: Path) -> BackupManifest:
    path = manifest_path_for(backup_path.resolve())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected_keys = set(BackupManifest.__dataclass_fields__)
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ValueError("manifest fields do not match the v1 contract")
        manifest = BackupManifest(
            format_version=int(raw["format_version"]),
            database_filename=str(raw["database_filename"]),
            schema_revision=str(raw["schema_revision"]),
            highest_event_sequence=int(raw["highest_event_sequence"]),
            blob_count=int(raw["blob_count"]),
            file_size=int(raw["file_size"]),
            sha256=str(raw["sha256"]),
            created_at_ms=int(raw["created_at_ms"]),
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupManifestError(f"Invalid backup manifest: {path}") from exc
    if manifest.format_version != BACKUP_FORMAT_VERSION:
        raise BackupManifestError(
            f"Unsupported backup manifest version: {manifest.format_version}."
        )
    if manifest.database_filename != backup_path.name:
        raise BackupManifestError("Manifest database filename does not match the backup path.")
    if len(manifest.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in manifest.sha256
    ):
        raise BackupManifestError("Manifest SHA-256 is malformed.")
    return manifest


def validate_backup(backup_path: Path) -> tuple[BackupManifest, DatabaseHealth]:
    backup = backup_path.resolve()
    manifest = read_backup_manifest(backup)
    if backup.stat().st_size != manifest.file_size:
        raise BackupManifestError("Backup size does not match its manifest.")
    if _sha256_file(backup) != manifest.sha256:
        raise BackupManifestError("Backup hash does not match its manifest.")
    health = validate_database(backup, expected_revision=manifest.schema_revision)
    if (
        health.highest_event_sequence != manifest.highest_event_sequence
        or health.blob_count != manifest.blob_count
    ):
        raise BackupManifestError("Backup health counters do not match its manifest.")
    return manifest, health


def alembic_config(database_path: Path) -> Config:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option("script_location", str(repository_root / "backend" / "alembic"))
    config.attributes["database_path"] = database_path
    return config


def restore_database(backup_path: Path, target_path: Path) -> DatabaseHealth:
    """Restore a verified snapshot while the FastAPI process is stopped."""
    backup = backup_path.resolve()
    target = target_path.resolve()
    validate_backup(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists():
            raise DatabaseHealthError(
                f"Restore requires a clean shutdown without SQLite sidecar: {sidecar}"
            )

    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    try:
        with closing(_connect_existing(backup)) as source_connection:
            with closing(sqlite3.connect(staging)) as destination_connection:
                source_connection.backup(destination_connection)
        command.upgrade(alembic_config(staging), "head")
        validate_database(staging)
        os.replace(staging, target)
        return validate_database(target)
    finally:
        staging.unlink(missing_ok=True)
