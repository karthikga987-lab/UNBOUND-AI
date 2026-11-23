"""FastAPI document endpoints.

Implements the routes described in docs/API_CONTRACT.md:
- POST /api/documents/upload
- GET  /api/documents
- POST /api/documents/search
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.documents.extractor import extract_document, infer_file_type, MAX_FILE_SIZE_BYTES
from backend.app.documents.models import ExtractionStatus
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.context import ContextIntelligence

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])


# Response/request models local to this API layer

class DocumentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    original_filename: str
    file_type: str
    extraction_status: str
    page_count: int | None = None
    chunk_count: int
    error: str | None = None


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    original_filename: str
    file_type: str
    upload_timestamp: str
    extraction_status: str
    page_count: int | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: list[str] | None = None


class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    filename: str
    file_type: str
    page: int | None = None
    section: str | None = None
    excerpt: str
    relevance_score: float
    matched_terms: list[str]
    evidence_id: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    results: list[SearchResultItem]
    total: int


# Dependency stubs – real app wires these at startup via app.state

def _get_store(request) -> DocumentStore:
    return request.app.state.document_store


def _get_searcher(request) -> DocumentSearcher:
    return request.app.state.document_searcher


def _get_intelligence(request) -> ContextIntelligence:
    return request.app.state.context_intelligence


def build_document_router(
    store: DocumentStore,
    searcher: DocumentSearcher,
    intelligence: ContextIntelligence,
) -> APIRouter:
    """
    Build an APIRouter with concrete service instances injected.

    Usage in main app:
        app.include_router(build_document_router(store, searcher, intel))
    """
    _router = APIRouter(prefix="/api/documents", tags=["documents"])

    @_router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
    async def upload_document(file: UploadFile = File(...)):
        """Upload and ingest a document (PDF, DOCX, TXT)."""
        # Multipart filenames are untrusted display metadata, never paths.
        filename = Path((file.filename or "upload").replace("\\", "/")).name or "upload"
        file_type = infer_file_type(filename)
        if file_type is None:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type. Supported: .pdf, .docx, .txt",
            )

        try:
            content = await file.read(MAX_FILE_SIZE_BYTES + 1)
        finally:
            await file.close()

        if len(content) == 0:
            raise HTTPException(status_code=422, detail="Uploaded file is empty")
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size: {MAX_FILE_SIZE_BYTES // (1024*1024)} MB",
            )

        document_id = str(uuid4())
        extraction = extract_document(content, document_id, filename)
        if extraction.status == ExtractionStatus.FAILED:
            raise HTTPException(
                status_code=422,
                detail="Invalid or unsupported document content",
            )

        try:
            document_id, stored_filename, stored_path = store.save_file(
                content, filename, document_id=document_id
            )
            store.save_extraction(
                document_id=document_id,
                original_filename=filename,
                stored_filename=stored_filename,
                file_path=stored_path,
                result=extraction,
            )
        except Exception as exc:
            logger.error("Document ingestion persistence failed: exception_class=%s", type(exc).__name__)
            if "stored_filename" in locals():
                store.cleanup_failed_ingestion(document_id, stored_filename)
            raise HTTPException(status_code=503, detail="Ingestion unavailable") from exc

        return DocumentUploadResponse(
            document_id=document_id,
            original_filename=filename,
            file_type=extraction.file_type.value,
            extraction_status=extraction.status.value,
            page_count=extraction.page_count,
            chunk_count=len(extraction.chunks),
            error=extraction.error,
        )

    @_router.get("", response_model=list[DocumentSummary])
    async def list_documents():
        """List all ingested documents."""
        records = store.list_records()
        return [
            DocumentSummary(
                document_id=r.document_id,
                original_filename=r.original_filename,
                file_type=r.file_type.value,
                upload_timestamp=r.upload_timestamp.isoformat(),
                extraction_status=r.extraction_status.value,
                page_count=r.page_count,
            )
            for r in records
        ]

    @_router.post("/search", response_model=SearchResponse)
    async def search_documents(request: SearchRequest):
        """Search indexed document content and return ranked excerpts."""
        import uuid as _uuid

        results = searcher.search(
            query=request.query,
            top_k=request.top_k,
            document_ids=request.document_ids,
        )

        items: list[SearchResultItem] = []
        for sr in results:
            chunk = sr.chunk
            rec = sr.document_record
            ev_id = str(_uuid.uuid4())
            items.append(
                SearchResultItem(
                    document_id=rec.document_id,
                    filename=rec.original_filename,
                    file_type=rec.file_type.value,
                    page=chunk.page,
                    section=chunk.section,
                    excerpt=chunk.text,
                    relevance_score=sr.score,
                    matched_terms=sr.matched_terms,
                    evidence_id=ev_id,
                )
            )

        return SearchResponse(
            query=request.query,
            results=items,
            total=len(items),
        )

    return _router
