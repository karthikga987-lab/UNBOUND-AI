import sqlite3
from io import BytesIO
import zipfile

import pytest

from backend.app.documents.extractor import extract_document
from backend.app.documents.store import DocumentStore
from backend.app.storage.s3_service import S3DocumentService
from backend.app.storage.s3_sync import S3SyncService
from backend.tests.test_s3_service import FakeS3


def make_db(path, table):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"CREATE TABLE {table}(value TEXT)")
    conn.execute(f"INSERT INTO {table} VALUES('synthetic')")
    conn.commit()
    return conn


def setup_service(tmp_path, *, client=None, enabled=True, interval=3600, prefix="sync"):
    tasks_path = tmp_path / "tasks.sqlite3"
    task_db = make_db(tasks_path, "tasks")
    docs = DocumentStore(tmp_path / "documents")
    raw = b"Synthetic approved document contents."
    doc_id, stored_name, file_path = docs.save_file(raw, "synthetic.txt")
    extraction = extract_document(raw, doc_id, "synthetic.txt")
    docs.save_extraction(doc_id, "synthetic.txt", stored_name, file_path, extraction)
    storage = S3DocumentService(client=client or FakeS3())
    service = S3SyncService(
        task_database=tasks_path,
        document_store=docs,
        storage=storage,
        state_path=tmp_path / "sync-state.sqlite3",
        enabled=enabled,
        interval_seconds=interval,
        retry_base_seconds=1,
        retry_max_seconds=4,
        sync_prefix=prefix,
    )
    return service, task_db


def test_application_snapshot_sync_includes_sqlite_and_supported_document_files(tmp_path):
    service, task_db = setup_service(tmp_path)
    try:
        payload, digest = service.create_snapshot()
        assert digest
        assert len(payload) > 0
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            assert "documents/files/" + service.document_store.list_records()[0].stored_filename in archive.namelist()
            temp_db = tmp_path / "snapshot-docs.sqlite3"
            temp_db.write_bytes(archive.read("documents/documents.sqlite3"))
            snapshot_db = sqlite3.connect(temp_db)
            try:
                paths = snapshot_db.execute("SELECT file_path,text_file_path FROM documents").fetchone()
            finally:
                snapshot_db.close()
            assert paths[0].startswith("files/")
            assert paths[1].startswith("texts/")
            assert str(tmp_path) not in paths[0] + paths[1]
        result = service.run_once(force=True)
        assert result["status"] == "synchronized"
        assert result["pending_count"] == 0
        assert result["last_sha256"] == digest
        objects = service.storage.list_objects(limit=10)
        assert len(objects) == 1
        assert objects[0].key.endswith(f"{digest}.zip")
        again = service.run_once(force=True)
        assert again["status"] == "synchronized"
        assert again["last_sha256"] == digest
        assert len(service.storage.list_objects(limit=10)) == 1
    finally:
        task_db.close()


def test_sync_retry_state_survives_service_recreation_and_idempotent_retry(tmp_path):
    class FailingOnce(FakeS3):
        fail = True

        def put_object(self, **kwargs):
            if self.fail:
                self.fail = False
                raise TimeoutError("synthetic timeout")
            super().put_object(**kwargs)

    client = FailingOnce()
    service, task_db = setup_service(tmp_path, client=client)
    try:
        failed = service.run_once(force=True)
        assert failed["status"] == "pending_retry"
        assert failed["pending_count"] == 1
        assert failed["retry_count"] == 1
        assert failed["last_error_code"] == "upload_failed"

        recovered = S3SyncService(
            task_database=service.task_database,
            document_store=service.document_store,
            storage=service.storage,
            state_path=tmp_path / "sync-state.sqlite3",
            enabled=True,
            interval_seconds=3600,
            retry_base_seconds=1,
            retry_max_seconds=4,
        ).run_once(force=True)
        assert recovered["status"] == "synchronized"
        assert recovered["pending_count"] == 0
        assert recovered["retry_count"] == 0
        assert len(client.objects) == 1
    finally:
        task_db.close()


def test_sync_disabled_by_default_and_does_not_attempt_upload(tmp_path):
    client = FakeS3()
    service, task_db = setup_service(tmp_path, client=client, enabled=False)
    try:
        with pytest.raises(Exception, match="disabled"):
            service.run_once(force=True)
        assert not client.calls
        assert service.get_status()["enabled"] is False
    finally:
        task_db.close()


def test_sync_prefix_is_configurable_but_remains_under_unbound(tmp_path):
    client = FakeS3()
    service, task_db = setup_service(tmp_path, client=client, prefix="phase5-verification")
    try:
        result = service.run_once(force=True)
        assert result["status"] == "synchronized"
        assert result["prefix"] == "unbound/phase5-verification/"
        assert result["last_object_key"].startswith("unbound/phase5-verification/snapshot-")
    finally:
        task_db.close()


def test_sync_prefix_rejects_traversal(tmp_path):
    service, task_db = setup_service(tmp_path)
    try:
        from backend.app.storage.s3_sync import S3SyncService
        with pytest.raises(Exception) as exc:
            S3SyncService(
                task_database=service.task_database,
                document_store=service.document_store,
                storage=service.storage,
                state_path=tmp_path / "bad-prefix-state.sqlite3",
                enabled=True,
                interval_seconds=3600,
                sync_prefix="../escape",
            )
        assert getattr(exc.value, "code", None) == "invalid_object_key"
    finally:
        task_db.close()


def test_application_snapshot_restore_verifies_and_writes_only_to_new_recovery_directory(tmp_path):
    client = FakeS3()
    service, task_db = setup_service(tmp_path, client=client)
    destination = tmp_path / "recovery-copy"
    try:
        synced = service.run_once(force=True)
        assert synced["status"] == "synchronized"
        restored = service.restore_snapshot_to_new_directory(
            synced["last_object_key"], destination, explicitly_approved=True,
        )
        assert restored["verified"] is True
        assert restored["integrity_check"] == "ok"
        assert (destination / "tasks.sqlite3").is_file()
        assert (destination / "documents" / "documents.sqlite3").is_file()
        assert list((destination / "documents" / "files").glob("*.txt"))
        with pytest.raises(Exception, match="must not already exist"):
            service.restore_snapshot_to_new_directory(
                synced["last_object_key"], destination, explicitly_approved=True,
            )
    finally:
        task_db.close()
