"""SQLite-backed document store with file-system text storage.

Large extracted text is written to flat files, not stored repeatedly in DB rows.
The database row holds a reference (file path) to the extracted text file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from backend.app.documents.models import (
    DocumentChunk,
    DocumentRecord,
    ExtractionResult,
    ExtractionStatus,
    FileType,
)
from backend.app.documents.extractor import MAX_FILE_SIZE_BYTES

logger = logging.getLogger(__name__)

# Local per-user storage; can be overridden explicitly by UNBOUND_DOCUMENT_STORAGE.
def default_storage_dir() -> Path:
    configured = os.getenv("UNBOUND_DOCUMENT_STORAGE")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "UNBOUND" / "documents").resolve()
    return (Path.home() / ".local" / "share" / "unbound" / "documents").resolve()


DEFAULT_STORAGE_DIR = default_storage_dir()
DB_FILENAME = "documents.db"

# DDL
_CREATE_DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    stored_filename   TEXT NOT NULL,
    file_path         TEXT NOT NULL,
    file_type         TEXT NOT NULL,
    upload_timestamp  TEXT NOT NULL,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    text_file_path    TEXT,
    page_count        INTEGER,
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_CHUNKS_TABLE = """
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    text        TEXT NOT NULL,
    page        INTEGER,
    section     TEXT,
    char_start  INTEGER NOT NULL DEFAULT 0,
    char_end    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);
"""


def _safe_filename(original: str, document_id: str) -> str:
    """Generate a safe stored filename that prevents path traversal.

    Returns a filename like ``<document_id>_<safe_stem><ext>``.
    """
    # Upload clients can submit Windows separators even when the server is not
    # running on Windows. Treat the name as display metadata, never as a path.
    path = Path(str(original).replace("\\", "/").split("/")[-1])
    ext = path.suffix.lower()
    if ext not in {".pdf", ".docx", ".txt"}:
        ext = ""
    # Keep only alphanumerics, hyphens and underscores from the stem
    stem = path.stem
    # Normalize unicode
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^\w\-]", "_", stem)[:40]
    return f"{document_id}_{stem}{ext}"


class DocumentStore:
    """
    Persistent store for uploaded documents.

    Documents are stored in ``storage_dir/files/`` with safe generated names.
    Extracted text is written to ``storage_dir/texts/`` as plain-text files.
    Document chunk metadata is stored in SQLite.
    """

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir = Path(storage_dir).expanduser().resolve() if storage_dir else default_storage_dir()
        self.files_dir = self.storage_dir / "files"
        self.texts_dir = self.storage_dir / "texts"
        self.db_path = self.storage_dir / DB_FILENAME

        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.texts_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_DOCUMENTS_TABLE)
            conn.execute(_CREATE_CHUNKS_TABLE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_file(
        self, file_bytes: bytes, original_filename: str, *, document_id: str | None = None
    ) -> tuple[str, str, str]:
        """
        Write raw file bytes to controlled storage with a safe generated name.

        Returns:
            (document_id, stored_filename, absolute_file_path)
        """
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            raise ValueError("Document exceeds the 20 MiB storage limit")
        document_id = document_id or str(uuid.uuid4())
        stored_filename = _safe_filename(original_filename, document_id)
        dest = self.files_dir / stored_filename

        # Resolve and confirm the destination is inside files_dir (path traversal guard)
        try:
            dest.resolve().relative_to(self.files_dir.resolve())
        except ValueError as exc:
            raise PermissionError(f"Path traversal detected: {dest}") from exc

        dest.write_bytes(file_bytes)
        return document_id, stored_filename, str(dest)

    def cleanup_failed_ingestion(self, document_id: str, stored_filename: str) -> None:
        """Remove only files created for a failed, uncommitted ingestion."""
        for base, name in ((self.files_dir, stored_filename), (self.texts_dir, f"{document_id}.txt")):
            candidate = (base / name).resolve()
            try:
                candidate.relative_to(base.resolve())
            except ValueError:
                logger.error("Refusing document cleanup outside storage boundary")
                continue
            try:
                candidate.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Document cleanup failed: exception_class=%s", type(exc).__name__)

    def save_extraction(
        self, document_id: str, original_filename: str, stored_filename: str,
        file_path: str, result: ExtractionResult,
    ) -> DocumentRecord:
        """Persist extraction results: writes text file and stores DB record."""
        text_file_path: str | None = None
        record = DocumentRecord(
            document_id=document_id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_path=file_path,
            file_type=result.file_type,
            upload_timestamp=datetime.now(timezone.utc),
            extraction_status=result.status,
            text_file_path=text_file_path,
            page_count=result.page_count,
            metadata=result.metadata,
        )

        try:
            # Check the persisted source file is inside the controlled store.
            Path(file_path).resolve().relative_to(self.files_dir.resolve())
            # Write extracted text separately to keep SQLite rows small.
            if result.full_text:
                text_dest = (self.texts_dir / f"{document_id}.txt").resolve()
                text_dest.relative_to(self.texts_dir.resolve())
                text_dest.write_text(result.full_text, encoding="utf-8")
                text_file_path = str(text_dest)
                record.text_file_path = text_file_path
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO documents
                    (document_id, original_filename, stored_filename, file_path,
                     file_type, upload_timestamp, extraction_status, text_file_path,
                     page_count, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.document_id, record.original_filename,
                        record.stored_filename, record.file_path,
                        record.file_type.value, record.upload_timestamp.isoformat(),
                        record.extraction_status.value, record.text_file_path,
                        record.page_count, json.dumps(record.metadata),
                    ),
                )
                # Store chunks atomically with the parent document record.
                for chunk in result.chunks:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO document_chunks
                        (chunk_id, document_id, text, page, section, char_start, char_end)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.chunk_id, chunk.document_id, chunk.text,
                            chunk.page, chunk.section, chunk.char_start, chunk.char_end,
                        ),
                    )
        except Exception:
            self.cleanup_failed_ingestion(document_id, stored_filename)
            raise
        return record

    def get_record(self, document_id: str) -> DocumentRecord | None:
        """Retrieve a document record by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_records(self) -> list[DocumentRecord]:
        """List all document records, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY upload_timestamp DESC"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        """Retrieve all chunks for a document."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM document_chunks WHERE document_id = ? ORDER BY char_start",
                (document_id,),
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_all_chunks(self) -> list[DocumentChunk]:
        """Retrieve all chunks across all documents (used by search)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM document_chunks ORDER BY document_id, char_start"
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def get_extracted_text(self, document_id: str) -> str | None:
        """Read the extracted text from disk for a given document."""
        record = self.get_record(document_id)
        if record is None or not record.text_file_path:
            return None
        text_path = Path(record.text_file_path)
        # Path traversal guard
        try:
            text_path.resolve().relative_to(self.texts_dir.resolve())
        except ValueError:
            logger.error("Text file path outside storage directory: %s", text_path)
            return None
        if not text_path.exists():
            return None
        return text_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            document_id=row["document_id"],
            original_filename=row["original_filename"],
            stored_filename=row["stored_filename"],
            file_path=row["file_path"],
            file_type=FileType(row["file_type"]),
            upload_timestamp=datetime.fromisoformat(row["upload_timestamp"]),
            extraction_status=ExtractionStatus(row["extraction_status"]),
            text_file_path=row["text_file_path"],
            page_count=row["page_count"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> DocumentChunk:
        return DocumentChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            text=row["text"],
            page=row["page"],
            section=row["section"],
            char_start=row["char_start"],
            char_end=row["char_end"],
        )
