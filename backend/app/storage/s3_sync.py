"""Opt-in, content-addressed S3 snapshots of task and document data.

Automatic upload is disabled unless explicitly enabled in the backend
environment. The service stores only sync metadata locally; source data remains
in its existing SQLite/document stores when S3 is unavailable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import threading
import shutil
import zipfile
from contextlib import contextmanager

from backend.app.storage.s3_service import S3DocumentService, S3StorageError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_sync_state_path() -> Path:
    configured = os.getenv("UNBOUND_S3_SYNC_STATE_DB")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "UNBOUND_AI" / "s3_sync.sqlite3"


class S3SyncStateStore:
    """Durable status for the latest whole-application snapshot attempt."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sync_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                status TEXT NOT NULL,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_error_code TEXT,
                pending_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_object_key TEXT,
                last_sha256 TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO sync_state(singleton,status) VALUES(1,'idle')")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self) -> dict[str, object]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM sync_state WHERE singleton=1").fetchone()
        return dict(row)

    def update(self, **values) -> None:
        allowed = {
            "status", "last_success_at", "last_attempt_at", "last_error_code",
            "pending_count", "retry_count", "next_retry_at", "last_object_key", "last_sha256",
        }
        if not values or not set(values).issubset(allowed):
            raise ValueError("Unsupported S3 sync state field")
        assignments = ",".join(f"{field}=?" for field in values)
        with self._lock, self._connection() as db:
            db.execute(f"UPDATE sync_state SET {assignments} WHERE singleton=1", tuple(values.values()))


class S3SyncService:
    """Snapshot task/evidence and indexed document data to private S3.

    Snapshot uploads are opt-in, immutable and idempotent by SHA-256. A failed
    attempt remains durably pending and is retried with capped exponential
    backoff. No remote object is deleted.
    """

    def __init__(
        self,
        *,
        task_database: str | Path,
        document_store,
        storage: S3DocumentService,
        state_path: str | Path | None = None,
        enabled: bool = False,
        interval_seconds: int = 3600,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 3600,
        sync_prefix: str = "sync",
    ) -> None:
        if not 60 <= interval_seconds <= 86400:
            raise ValueError("interval_seconds must be between 60 and 86400")
        if not 1 <= retry_base_seconds <= retry_max_seconds <= 86400:
            raise ValueError("retry backoff configuration is invalid")
        self.task_database = Path(task_database).expanduser().resolve()
        self.document_store = document_store
        self.storage = storage
        normalized_prefix = sync_prefix.strip("/") if isinstance(sync_prefix, str) else ""
        self.sync_prefix = self.storage.object_key(f"{normalized_prefix}/snapshot-probe.zip").rsplit("/", 1)[0]
        self.state = S3SyncStateStore(state_path or default_sync_state_path())
        self.enabled = enabled is True
        self.interval_seconds = interval_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _consistent_database_snapshot(path: Path, max_bytes: int) -> bytes:
        if not path.is_file():
            raise S3StorageError("database_not_found", "A required application database was not found")
        import tempfile
        with tempfile.TemporaryDirectory(prefix="unbound-s3-sync-") as directory:
            snapshot_path = Path(directory) / "snapshot.sqlite3"
            try:
                source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
                target = sqlite3.connect(snapshot_path)
                try:
                    source.execute("PRAGMA busy_timeout=10000")
                    source.backup(target, pages=128, sleep=0.05)
                    check = target.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise S3StorageError("snapshot_integrity_failed", "Application database snapshot failed integrity verification")
                finally:
                    target.close()
                    source.close()
                if snapshot_path.stat().st_size > max_bytes:
                    raise S3StorageError("file_size_limit_exceeded", "Application database exceeds the configured synchronization limit")
                return snapshot_path.read_bytes()
            except S3StorageError:
                raise
            except (OSError, sqlite3.Error):
                raise S3StorageError("snapshot_failed", "Application database snapshot could not be created safely") from None

    def _document_payloads(self) -> tuple[bytes, list[tuple[str, bytes]], dict[str, object]]:
        store = self.document_store
        db_snapshot = self._portable_documents_database(self._consistent_database_snapshot(Path(store.db_path), self.storage.max_bytes))
        payloads: list[tuple[str, bytes]] = []
        manifest_records: list[dict[str, object]] = []
        total_bytes = len(db_snapshot)
        files_root = Path(store.files_dir).resolve()
        texts_root = Path(store.texts_dir).resolve()
        for record in store.list_records():
            entry: dict[str, object] = {
                "document_id": record.document_id,
                "original_filename": record.original_filename,
                "stored_filename": record.stored_filename,
                "file_type": str(record.file_type),
                "upload_timestamp": record.upload_timestamp.isoformat(),
                "extraction_status": str(record.extraction_status),
                "page_count": record.page_count,
                "metadata": record.metadata,
                "files": [],
            }
            sources = [(Path(record.file_path), f"documents/files/{record.stored_filename}")]
            if record.text_file_path:
                sources.append((Path(record.text_file_path), f"documents/texts/{record.document_id}.txt"))
            for source_path, archive_name in sources:
                try:
                    resolved = source_path.resolve(strict=True)
                    root = files_root if archive_name.startswith("documents/files/") else texts_root
                    resolved.relative_to(root)
                    before = resolved.stat()
                    if before.st_size > self.storage.max_bytes - total_bytes:
                        raise S3StorageError("file_size_limit_exceeded", "Document files exceed the configured synchronization limit")
                    content = resolved.read_bytes()
                    after = resolved.stat()
                except (OSError, ValueError):
                    raise S3StorageError("document_snapshot_failed", "A stored document could not be snapshotted safely") from None
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != after.st_size:
                    raise S3StorageError("document_changed_during_snapshot", "A document changed during snapshot; no upload was reported")
                digest = sha256(content).hexdigest()
                payloads.append((archive_name, content))
                total_bytes += len(content)
                entry["files"].append({"path": archive_name, "size_bytes": len(content), "sha256": digest})
            manifest_records.append(entry)
        manifest = {
            "format": "unbound-document-sync",
            "format_version": 1,
            "documents_database_sha256": sha256(db_snapshot).hexdigest(),
            "documents": manifest_records,
        }
        return db_snapshot, payloads, manifest

    @staticmethod
    def _portable_documents_database(snapshot: bytes) -> bytes:
        """Replace machine-specific absolute file paths in the archive copy."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unbound-docdb-portable-") as directory:
            path = Path(directory) / "documents.sqlite3"
            path.write_bytes(snapshot)
            try:
                connection = sqlite3.connect(path)
                try:
                    connection.execute(
                        "UPDATE documents SET file_path='files/' || stored_filename, "
                        "text_file_path=CASE WHEN text_file_path IS NULL THEN NULL "
                        "ELSE 'texts/' || document_id || '.txt' END"
                    )
                    connection.commit()
                    result = connection.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        raise S3StorageError("snapshot_integrity_failed", "Portable document snapshot failed integrity verification")
                finally:
                    connection.close()
                return path.read_bytes()
            except S3StorageError:
                raise
            except sqlite3.Error:
                raise S3StorageError("snapshot_failed", "Document database could not be prepared for portable synchronization") from None

    def create_snapshot(self) -> tuple[bytes, str]:
        """Create a bounded archive containing consistent DBs and stored docs."""
        task_db = self._consistent_database_snapshot(self.task_database, self.storage.max_bytes)
        docs_db, document_files, docs_manifest = self._document_payloads()
        if len(task_db) + len(docs_db) + sum(len(item[1]) for item in document_files) > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "Application snapshot exceeds the configured S3 synchronization limit")
        manifest = {
            "format": "unbound-application-sync",
            "format_version": 1,
            "task_database_sha256": sha256(task_db).hexdigest(),
            "documents_database_sha256": sha256(docs_db).hexdigest(),
            "documents": docs_manifest["documents"],
        }
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add_entry(name: str, content: bytes) -> None:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)

            add_entry("manifest.json", json.dumps(manifest, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8"))
            add_entry("tasks.sqlite3", task_db)
            add_entry("documents/documents.sqlite3", docs_db)
            for name, content in document_files:
                add_entry(name, content)
        content = output.getvalue()
        if len(content) > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "Compressed application snapshot exceeds the S3 synchronization limit")
        return content, sha256(content).hexdigest()

    def restore_snapshot_to_new_directory(
        self,
        object_name: str,
        destination: str | Path,
        *,
        explicitly_approved: bool = False,
    ) -> dict[str, object]:
        """Verify and restore an application snapshot into a new recovery folder."""
        if explicitly_approved is not True:
            raise S3StorageError("approval_required", "Explicit confirmation is required before restore")
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise S3StorageError("restore_target_exists", "Recovery destination must not already exist")

        buffer = BytesIO()
        self.storage.download_to_fileobj(object_name, buffer)
        payload = buffer.getvalue()
        if len(payload) > self.storage.max_bytes:
            raise S3StorageError("file_size_limit_exceeded", "Application snapshot exceeds the configured restore limit")
        digest = sha256(payload).hexdigest()
        expected_digest = Path(object_name).name.removeprefix("snapshot-").removesuffix(".zip")
        if len(expected_digest) != 64 or digest != expected_digest:
            raise S3StorageError("backup_checksum_failed", "Application snapshot checksum did not match its object key")

        try:
            archive = zipfile.ZipFile(BytesIO(payload), "r")
        except (zipfile.BadZipFile, OSError):
            raise S3StorageError("invalid_backup", "S3 object is not a valid application snapshot") from None
        with archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise S3StorageError("invalid_backup", "Application snapshot contains invalid or duplicate entries")
            if any(item.file_size < 0 or item.file_size > self.storage.max_bytes for item in infos):
                raise S3StorageError("file_size_limit_exceeded", "Application snapshot member exceeds the restore limit")
            if sum(item.file_size for item in infos) > self.storage.max_bytes:
                raise S3StorageError("file_size_limit_exceeded", "Application snapshot contents exceed the restore limit")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, ValueError, UnicodeDecodeError, RuntimeError, zipfile.BadZipFile):
                raise S3StorageError("invalid_backup", "Application snapshot manifest is invalid") from None
            if not isinstance(manifest, dict) or manifest.get("format") != "unbound-application-sync" or manifest.get("format_version") != 1:
                raise S3StorageError("invalid_backup", "Application snapshot format is unsupported")
            required = {"manifest.json", "tasks.sqlite3", "documents/documents.sqlite3"}
            expected_hashes = {
                "tasks.sqlite3": manifest.get("task_database_sha256"),
                "documents/documents.sqlite3": manifest.get("documents_database_sha256"),
            }
            expected_sizes: dict[str, int] = {}
            documents = manifest.get("documents")
            if not isinstance(documents, list):
                raise S3StorageError("invalid_backup", "Application snapshot document manifest is invalid")
            for document in documents:
                if not isinstance(document, dict) or not isinstance(document.get("files"), list):
                    raise S3StorageError("invalid_backup", "Application snapshot document manifest is invalid")
                for file_record in document["files"]:
                    if not isinstance(file_record, dict):
                        raise S3StorageError("invalid_backup", "Application snapshot file manifest is invalid")
                    name = file_record.get("path")
                    if not isinstance(name, str) or not (
                        name.startswith("documents/files/") or name.startswith("documents/texts/")
                    ):
                        raise S3StorageError("invalid_backup", "Application snapshot contains an unsupported file path")
                    relative = Path(name)
                    if (
                        relative.is_absolute() or len(relative.parts) != 3
                        or relative.parts[1] not in {"files", "texts"}
                        or relative.parts[2] in {"", ".", ".."}
                        or any(character in name for character in ("\\", "\x00", ":"))
                    ):
                        raise S3StorageError("invalid_backup", "Application snapshot contains an unsafe file path")
                    file_digest = file_record.get("sha256")
                    file_size = file_record.get("size_bytes")
                    if (
                        not isinstance(file_digest, str) or len(file_digest) != 64
                        or not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 0
                    ):
                        raise S3StorageError("invalid_backup", "Application snapshot file metadata is invalid")
                    if name in expected_hashes:
                        raise S3StorageError("invalid_backup", "Application snapshot manifest contains a duplicate file")
                    required.add(name)
                    expected_hashes[name] = file_digest
                    expected_sizes[name] = file_size
            if set(names) != required or not all(isinstance(value, str) for value in expected_hashes.values()):
                raise S3StorageError("invalid_backup", "Application snapshot contents do not match its manifest")

            contents: dict[str, bytes] = {}
            for name, expected in expected_hashes.items():
                try:
                    data = archive.read(name)
                except (KeyError, RuntimeError, zipfile.BadZipFile):
                    raise S3StorageError("invalid_backup", "Application snapshot member could not be verified") from None
                if sha256(data).hexdigest() != expected or (
                    name in expected_sizes and len(data) != expected_sizes[name]
                ):
                    raise S3StorageError("backup_checksum_failed", "Application snapshot member checksum did not match")
                contents[name] = data

        import tempfile

        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            raise S3StorageError("restore_target_exists", "Recovery destination must not already exist")
        temp_root = Path(tempfile.mkdtemp(prefix="unbound-s3-restore-", dir=target.parent))
        try:
            for name, data in contents.items():
                output = temp_root.joinpath(*Path(name).parts)
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                output.write_bytes(data)
            for name in ("tasks.sqlite3", "documents/documents.sqlite3"):
                db = sqlite3.connect(f"file:{(temp_root / name).as_posix()}?mode=ro", uri=True)
                try:
                    integrity = db.execute("PRAGMA integrity_check").fetchone()
                finally:
                    db.close()
                if not integrity or integrity[0] != "ok":
                    raise S3StorageError("restore_integrity_failed", "Restored application database failed its integrity check")
            os.rename(temp_root, target)
        except S3StorageError:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        except (OSError, sqlite3.Error):
            shutil.rmtree(temp_root, ignore_errors=True)
            raise S3StorageError("restore_failed", "Application snapshot could not be restored safely") from None
        return {
            "destination": str(target),
            "files_restored": len(contents),
            "snapshot_sha256": digest,
            "integrity_check": "ok",
            "verified": True,
        }

    def get_status(self) -> dict[str, object]:
        result = self.state.get()
        result.update({
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "bucket": self.storage.bucket,
            "region": self.storage.region,
            "prefix": f"{self.sync_prefix}/",
        })
        return result

    def run_once(self, *, force: bool = False) -> dict[str, object]:
        if not self.enabled:
            raise S3StorageError("sync_disabled", "S3 synchronization is disabled; explicit backend opt-in is required")
        if not self._run_lock.acquire(blocking=False):
            return self.get_status()
        try:
            now = _utc_now()
            prior = self.state.get()
            next_retry = prior.get("next_retry_at")
            if not force and next_retry:
                try:
                    if now < datetime.fromisoformat(str(next_retry)):
                        return prior | {"enabled": self.enabled, "interval_seconds": self.interval_seconds,
                                        "bucket": self.storage.bucket, "region": self.storage.region,
                                        "prefix": f"{self.sync_prefix}/"}
                except ValueError:
                    pass
            last_success = prior.get("last_success_at")
            if not force and last_success and not prior.get("pending_count"):
                try:
                    if now - datetime.fromisoformat(str(last_success)) < timedelta(seconds=self.interval_seconds):
                        return self.get_status()
                except ValueError:
                    pass
            self.state.update(status="syncing", last_attempt_at=now.isoformat(), last_error_code=None)
            try:
                payload, digest = self.create_snapshot()
                key = f"{self.sync_prefix}/snapshot-{digest}.zip"
                try:
                    receipt = self.storage.upload_bytes(
                        key, payload, explicitly_approved=True,
                        content_type="application/vnd.unbound.application-snapshot+zip",
                    )
                except S3StorageError as exc:
                    if exc.code != "object_exists":
                        raise
                    self.storage.verify_existing_object(key, size_bytes=len(payload), sha256_digest=digest)
                    receipt = type("VerifiedReceipt", (), {"key": self.storage.object_key(key), "sha256": digest, "verified": True})()
                self.state.update(
                    status="synchronized", last_success_at=now.isoformat(), last_attempt_at=now.isoformat(),
                    last_error_code=None, pending_count=0, retry_count=0, next_retry_at=None,
                    last_object_key=receipt.key, last_sha256=receipt.sha256,
                )
            except S3StorageError as exc:
                retry_count = min(int(prior.get("retry_count") or 0) + 1, 16)
                delay = min(self.retry_base_seconds * (2 ** (retry_count - 1)), self.retry_max_seconds)
                self.state.update(
                    status="pending_retry", last_attempt_at=now.isoformat(), last_error_code=exc.code,
                    pending_count=1, retry_count=retry_count, next_retry_at=(now + timedelta(seconds=delay)).isoformat(),
                )
            except Exception:
                retry_count = min(int(prior.get("retry_count") or 0) + 1, 16)
                delay = min(self.retry_base_seconds * (2 ** (retry_count - 1)), self.retry_max_seconds)
                self.state.update(
                    status="pending_retry", last_attempt_at=now.isoformat(), last_error_code="sync_internal_error",
                    pending_count=1, retry_count=retry_count, next_retry_at=(now + timedelta(seconds=delay)).isoformat(),
                )
            return self.get_status()
        finally:
            self._run_lock.release()

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="unbound-s3-sync", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Status handling lives in run_once; never kill the application loop.
                pass
            self._stop.wait(min(30, self.interval_seconds))

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
