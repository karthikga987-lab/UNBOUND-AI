"""Explicit, integrity-checked SQLite snapshots for private S3 backup.

This is a backup tool, not bidirectional synchronization. Upload and restore
both require explicit caller confirmation. Restore always targets a new path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from backend.app.storage.s3_service import DEFAULT_MAX_BYTES, S3DocumentService, S3StorageError


_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "database.sqlite3"
_FORMAT_VERSION = 1


@dataclass(frozen=True)
class SQLiteBackupReceipt:
    key: str
    database_size_bytes: int
    database_sha256: str
    object_sha256: str
    verified: bool


class SQLiteBackupService:
    """Take SQLite's consistent online snapshot and store one bounded archive."""

    def __init__(self, storage: S3DocumentService) -> None:
        self.storage = storage

    @staticmethod
    def _snapshot(database_path: str | Path) -> bytes:
        source_path = Path(database_path).expanduser()
        if not source_path.is_file():
            raise S3StorageError("database_not_found", "SQLite database was not found")
        try:
            with tempfile.TemporaryDirectory(prefix="unbound-sqlite-snapshot-") as directory:
                target_path = Path(directory) / _DATABASE_NAME
                source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
                target = sqlite3.connect(target_path)
                try:
                    source.execute("PRAGMA busy_timeout=10000")
                    source.backup(target, pages=128, sleep=0.05)
                    result = target.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        raise S3StorageError("snapshot_integrity_failed", "SQLite snapshot failed its integrity check")
                finally:
                    target.close()
                    source.close()
                content = target_path.read_bytes()
        except S3StorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise S3StorageError("snapshot_failed", "SQLite snapshot could not be created safely") from exc
        return content

    def create_archive(self, database_path: str | Path) -> tuple[bytes, dict[str, object]]:
        database = self._snapshot(database_path)
        if not database or len(database) > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "SQLite snapshot exceeds the configured S3 backup limit")
        manifest: dict[str, object] = {
            "format": "unbound-sqlite-backup",
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_name": _DATABASE_NAME,
            "database_size_bytes": len(database),
            "database_sha256": sha256(database).hexdigest(),
        }
        archive_buffer = BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(_MANIFEST_NAME, json.dumps(manifest, separators=(",", ":")))
            archive.writestr(_DATABASE_NAME, database)
        payload = archive_buffer.getvalue()
        if len(payload) > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "SQLite backup archive exceeds the configured S3 limit")
        return payload, manifest

    def upload_database(
        self,
        database_path: str | Path,
        object_name: str,
        *,
        explicitly_approved: bool = False,
    ) -> SQLiteBackupReceipt:
        if explicitly_approved is not True:
            raise S3StorageError("approval_required", "Explicit user confirmation is required before database backup")
        key = object_name if object_name.startswith("backups/") else f"backups/{object_name}"
        payload, manifest = self.create_archive(database_path)
        receipt = self.storage.upload_bytes(
            key,
            payload,
            explicitly_approved=True,
            content_type="application/vnd.unbound.sqlite-backup+zip",
        )
        return SQLiteBackupReceipt(
            key=receipt.key,
            database_size_bytes=int(manifest["database_size_bytes"]),
            database_sha256=str(manifest["database_sha256"]),
            object_sha256=receipt.sha256,
            verified=receipt.verified,
        )

    def restore_to_new_path(
        self,
        object_name: str,
        destination: str | Path,
        *,
        active_database_path: str | Path,
        explicitly_approved: bool = False,
    ) -> dict[str, object]:
        if explicitly_approved is not True:
            raise S3StorageError("approval_required", "Explicit confirmation is required before restore")
        target = Path(destination).expanduser().resolve()
        active = Path(active_database_path).expanduser().resolve()
        if target == active:
            raise S3StorageError("restore_target_is_active", "Restore cannot overwrite the active database")
        if target.exists():
            raise S3StorageError("restore_target_exists", "Restore destination must not already exist")
        archive_bytes = BytesIO()
        info = self.storage.download_to_fileobj(object_name, archive_bytes)
        if info.size_bytes > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "S3 backup exceeds the configured restore limit")
        archive_bytes.seek(0)
        try:
            with zipfile.ZipFile(archive_bytes, "r") as archive:
                names = archive.namelist()
                if sorted(names) != sorted([_MANIFEST_NAME, _DATABASE_NAME]):
                    raise S3StorageError("invalid_backup", "Backup contents do not match the supported format")
                if any(archive.getinfo(name).file_size > self.storage.max_bytes for name in names):
                    raise S3StorageError("file_size_limit_exceeded", "S3 backup member exceeds the configured restore limit")
                manifest_data = json.loads(archive.read(_MANIFEST_NAME))
                if (
                    manifest_data.get("format") != "unbound-sqlite-backup"
                    or manifest_data.get("format_version") != _FORMAT_VERSION
                    or manifest_data.get("database_name") != _DATABASE_NAME
                ):
                    raise S3StorageError("invalid_backup", "Backup metadata is unsupported")
                database = archive.read(_DATABASE_NAME)
        except S3StorageError:
            raise
        except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
            raise S3StorageError("invalid_backup", "S3 object is not a valid UNBOUND SQLite backup") from exc
        digest = sha256(database).hexdigest()
        if (
            len(database) != manifest_data.get("database_size_bytes")
            or digest != manifest_data.get("database_sha256")
            or len(database) > self.storage.max_bytes
        ):
            raise S3StorageError("backup_checksum_failed", "SQLite backup checksum or size did not match its manifest")
        created = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as output:
                output.write(database)
                output.flush()
                os.fsync(output.fileno())
            check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
            try:
                integrity = check.execute("PRAGMA integrity_check").fetchone()
            finally:
                check.close()
            if not integrity or integrity[0] != "ok":
                raise S3StorageError("restore_integrity_failed", "Restored database failed its integrity check")
        except S3StorageError:
            if created:
                target.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            if created:
                target.unlink(missing_ok=True)
            raise S3StorageError("restore_failed", "SQLite backup could not be restored safely") from exc
        return {
            "destination": str(target),
            "database_size_bytes": len(database),
            "database_sha256": digest,
            "integrity_check": "ok",
            "verified": True,
        }
