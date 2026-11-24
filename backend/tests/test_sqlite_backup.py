import sqlite3
from io import BytesIO
import json
import zipfile

import pytest

from backend.app.storage.s3_service import S3DocumentService, S3StorageError
from backend.app.storage.sqlite_backup import SQLiteBackupService
from backend.tests.test_s3_service import FakeS3


def make_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
    connection.execute("INSERT INTO sample VALUES('synthetic backup row')")
    connection.commit()
    return connection


def test_sqlite_online_snapshot_s3_upload_and_separate_restore(tmp_path):
    source_path = tmp_path / "active.sqlite3"
    source = make_database(source_path)
    try:
        # Keep the source connection open in WAL mode while the backup API runs.
        service = S3DocumentService(client=FakeS3())
        backups = SQLiteBackupService(service)
        with pytest.raises(S3StorageError) as exc:
            backups.upload_database(source_path, "demo/backup.zip")
        assert exc.value.code == "approval_required"

        receipt = backups.upload_database(source_path, "demo/backup.zip", explicitly_approved=True)
        assert receipt.verified is True
        assert receipt.database_size_bytes > 0
        assert len(receipt.database_sha256) == 64
        assert receipt.key.startswith("unbound/backups/")

        with pytest.raises(S3StorageError) as exc:
            backups.restore_to_new_path(
                receipt.key,
                source_path,
                active_database_path=source_path,
                explicitly_approved=True,
            )
        assert exc.value.code == "restore_target_is_active"

        restored_path = tmp_path / "restored" / "recovered.sqlite3"
        result = backups.restore_to_new_path(
            receipt.key,
            restored_path,
            active_database_path=source_path,
            explicitly_approved=True,
        )
        assert result["verified"] is True
        assert result["integrity_check"] == "ok"
        restored = sqlite3.connect(restored_path)
        try:
            assert restored.execute("SELECT value FROM sample").fetchone()[0] == "synthetic backup row"
        finally:
            restored.close()
    finally:
        source.close()


def test_restore_does_not_overwrite_existing_destination(tmp_path):
    source_path = tmp_path / "active.sqlite3"
    source = make_database(source_path)
    destination = tmp_path / "restore.sqlite3"
    destination.write_bytes(b"existing user file")
    try:
        service = S3DocumentService(client=FakeS3())
        backups = SQLiteBackupService(service)
        receipt = backups.upload_database(source_path, "backup.zip", explicitly_approved=True)
        with pytest.raises(S3StorageError) as exc:
            backups.restore_to_new_path(
                receipt.key,
                destination,
                active_database_path=source_path,
                explicitly_approved=True,
            )
        assert exc.value.code == "restore_target_exists"
        assert destination.read_bytes() == b"existing user file"
    finally:
        source.close()


def test_restore_rejects_corrupt_snapshot_checksum_without_creating_target(tmp_path):
    source_path = tmp_path / "active.sqlite3"
    source = make_database(source_path)
    try:
        client = FakeS3()
        storage = S3DocumentService(client=client)
        backups = SQLiteBackupService(storage)
        _payload, manifest = backups.create_archive(source_path)
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("database.sqlite3", b"corrupt bytes")
        key = "unbound/backups/corrupt.zip"
        corrupt_archive = buffer.getvalue()
        client.objects[(storage.bucket, key)] = (
            corrupt_archive,
            {"sha256": "irrelevant-for-this-integrity-check"},
        )
        target = tmp_path / "recovery.sqlite3"
        with pytest.raises(S3StorageError) as exc:
            backups.restore_to_new_path(
                key,
                target,
                active_database_path=source_path,
                explicitly_approved=True,
            )
        assert exc.value.code == "backup_checksum_failed"
        assert not target.exists()
    finally:
        source.close()
