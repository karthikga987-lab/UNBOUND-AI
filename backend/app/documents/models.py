"""Document metadata and extraction result models (not shared contracts)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    EMPTY = "empty"


class DocumentChunk(BaseModel):
    """A retrievable excerpt with its source reference."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(default_factory=lambda: str(uuid4()))
    document_id: str
    text: str
    page: int | None = None  # 1-based for PDFs, None for flat text
    section: str | None = None  # heading/section name for DOCX
    char_start: int = 0
    char_end: int = 0


class ExtractionResult(BaseModel):
    """Full extraction output for a document."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    filename: str
    file_type: FileType
    full_text: str
    chunks: list[DocumentChunk] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    error: str | None = None
    page_count: int | None = None


class DocumentRecord(BaseModel):
    """Persisted document row (stored in SQLite)."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(default_factory=lambda: str(uuid4()))
    original_filename: str
    stored_filename: str
    file_path: str
    file_type: FileType
    upload_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extraction_status: ExtractionStatus = ExtractionStatus.PENDING
    text_file_path: str | None = None  # Path to extracted text file
    page_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """A ranked search result backed by a document chunk."""

    model_config = ConfigDict(extra="forbid")

    chunk: DocumentChunk
    document_record: DocumentRecord
    score: float
    matched_terms: list[str] = Field(default_factory=list)
