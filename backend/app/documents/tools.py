"""Tool definitions and callables for document and context tools.

Serializable ToolDefinition objects are kept separate from executable callables,
per the TOOL_CONTRACT specification. The callable interface is:
    execute(validated_arguments: BaseModel, tool_call: ToolCall) -> ToolResult
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.documents.extractor import extract_document, infer_file_type
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.context import ContextIntelligence
from backend.app.schemas.contracts import (
    EvidenceItem,
    EvidenceSourceType,
    ToolCall,
    ToolDefinition,
    ToolResult,
    utc_now,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input models (Pydantic 2) – these generate the input_schema for ToolDefinition
# ---------------------------------------------------------------------------

class DocumentUploadInput(BaseModel):
    """Input for document.upload tool."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(
        description="Absolute path to the file to upload and ingest. Must be .pdf, .docx, or .txt."
    )
    original_filename: str = Field(
        description="Original filename including extension, used for display and type detection."
    )


class DocumentSearchInput(BaseModel):
    """Input for document.search tool."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Free-text search query.")
    top_k: int = Field(default=5, ge=1, le=20, description="Maximum number of results to return.")
    document_ids: list[str] | None = Field(
        default=None,
        description="Optional list of document IDs to restrict the search to.",
    )


class ContextRetrieveInput(BaseModel):
    """Input for context.retrieve tool."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Query to retrieve relevant context for.")
    top_k: int = Field(default=5, ge=1, le=20, description="Number of relevant excerpts to return.")
    document_ids: list[str] | None = Field(
        default=None,
        description="Optional list of document IDs to restrict the search to.",
    )


class ContextRelateInput(BaseModel):
    """Input for context.relate tool."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="Topic or question to find relationships for.")
    top_k: int = Field(default=10, ge=1, le=30, description="Number of source chunks to analyze.")
    document_ids: list[str] | None = Field(
        default=None,
        description="Optional list of document IDs to restrict the analysis to.",
    )


# ---------------------------------------------------------------------------
# ToolDefinition singletons (serializable, no callables)
# ---------------------------------------------------------------------------

DOCUMENT_UPLOAD_DEFINITION = ToolDefinition(
    tool_name="document.upload",
    description=(
        "Upload and ingest a document file (PDF, DOCX, or TXT). "
        "Extracts text, stores the document, and returns its document ID and extraction status."
    ),
    input_schema=DocumentUploadInput.model_json_schema(),
    requires_approval=False,
)

DOCUMENT_SEARCH_DEFINITION = ToolDefinition(
    tool_name="document.search",
    description=(
        "Search indexed document content using a free-text query. "
        "Returns the most relevant excerpts with source filename, page/section, "
        "and EvidenceItem provenance."
    ),
    input_schema=DocumentSearchInput.model_json_schema(),
    requires_approval=False,
)

CONTEXT_RETRIEVE_DEFINITION = ToolDefinition(
    tool_name="context.retrieve",
    description=(
        "Retrieve relevant document excerpts and return structured EvidenceItem objects "
        "traceable to their source. Use this to supply the orchestrator with evidence-backed context."
    ),
    input_schema=ContextRetrieveInput.model_json_schema(),
    requires_approval=False,
)

CONTEXT_RELATE_DEFINITION = ToolDefinition(
    tool_name="context.relate",
    description=(
        "Find shared entities (companies, dates, metrics) across multiple document sources for a topic. "
        "Returns explicitly labelled inferred relationships. "
        "Co-occurrence does not imply causation."
    ),
    input_schema=ContextRelateInput.model_json_schema(),
    requires_approval=False,
)

ALL_DEFINITIONS = [
    DOCUMENT_UPLOAD_DEFINITION,
    DOCUMENT_SEARCH_DEFINITION,
    CONTEXT_RETRIEVE_DEFINITION,
    CONTEXT_RELATE_DEFINITION,
]


# ---------------------------------------------------------------------------
# Tool factory – produces bound callables from service instances
# ---------------------------------------------------------------------------

class DocumentContextTools:
    """
    Factory that binds document and context service instances to tool callables.

    Usage:
        store = DocumentStore(storage_dir)
        searcher = DocumentSearcher(store)
        intel = ContextIntelligence(store, searcher)
        tools = DocumentContextTools(store, searcher, intel)
        # Register with ToolRegistry:
        registry.register(DOCUMENT_UPLOAD_DEFINITION, DocumentUploadInput, tools.execute_upload)
        ...
    """

    def __init__(
        self,
        store: DocumentStore,
        searcher: DocumentSearcher,
        intelligence: ContextIntelligence,
    ) -> None:
        self._store = store
        self._searcher = searcher
        self._intelligence = intelligence

    # ------------------------------------------------------------------
    # document.upload
    # ------------------------------------------------------------------

    def execute_upload(self, args: DocumentUploadInput, call: ToolCall) -> ToolResult:
        """
        Ingest a file from the filesystem into document storage.

        The file at args.file_path is treated as untrusted content.
        No macros or scripts are executed. File type is validated by extension.
        """
        file_path = Path(args.file_path)

        # Validate file type before reading
        file_type = infer_file_type(args.original_filename)
        if file_type is None:
            return ToolResult(
                success=False,
                error=f"Unsupported file type for '{args.original_filename}'. Supported: .pdf, .docx, .txt",
            )

        if not file_path.exists():
            return ToolResult(
                success=False,
                error=f"File not found: {args.file_path}",
            )

        try:
            file_bytes = file_path.read_bytes()
        except Exception as exc:
            return ToolResult(success=False, error=f"Could not read file: {exc}")

        try:
            document_id, stored_filename, stored_path = self._store.save_file(
                file_bytes, args.original_filename
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to store file: {exc}")

        extraction = extract_document(file_bytes, document_id, args.original_filename)

        try:
            record = self._store.save_extraction(
                document_id=document_id,
                original_filename=args.original_filename,
                stored_filename=stored_filename,
                file_path=stored_path,
                result=extraction,
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Failed to save extraction: {exc}")

        evidence = EvidenceItem(
            task_id=call.task_id,
            source_type=EvidenceSourceType.DOCUMENT,
            source_name=args.original_filename,
            file_path=stored_path,
            excerpt=extraction.full_text[:500] if extraction.full_text else None,
            retrieved_at=utc_now(),
            supports_claim=(
                f"Document '{args.original_filename}' was uploaded, extracted "
                f"(status: {extraction.status.value}), and stored with ID {document_id}."
            ),
        )

        return ToolResult(
            success=extraction.status.value in ("success", "empty"),
            data={
                "document_id": document_id,
                "original_filename": args.original_filename,
                "file_type": extraction.file_type.value,
                "extraction_status": extraction.status.value,
                "page_count": extraction.page_count,
                "chunk_count": len(extraction.chunks),
                "stored_path": stored_path,
                "error": extraction.error,
            },
            evidence=[evidence],
            error=extraction.error if extraction.status.value == "failed" else None,
        )

    # ------------------------------------------------------------------
    # document.search
    # ------------------------------------------------------------------

    def execute_search(self, args: DocumentSearchInput, call: ToolCall) -> ToolResult:
        """Search indexed chunks and return ranked excerpts with evidence."""
        results = self._searcher.search(
            query=args.query,
            top_k=args.top_k,
            document_ids=args.document_ids,
        )

        excerpts: list[dict[str, Any]] = []
        evidence: list[EvidenceItem] = []

        for sr in results:
            chunk = sr.chunk
            rec = sr.document_record
            source_ref = (
                f"page {chunk.page}" if chunk.page else
                (f"section '{chunk.section}'" if chunk.section else "body")
            )
            ev = EvidenceItem(
                task_id=call.task_id,
                source_type=EvidenceSourceType.DOCUMENT,
                source_name=rec.original_filename,
                file_path=rec.file_path,
                excerpt=chunk.text[:1000],
                retrieved_at=utc_now(),
                supports_claim=(
                    f"'{rec.original_filename}' ({source_ref}) matches query: {args.query!r}"
                ),
            )
            evidence.append(ev)
            excerpts.append({
                "document_id": rec.document_id,
                "filename": rec.original_filename,
                "file_type": rec.file_type.value,
                "page": chunk.page,
                "section": chunk.section,
                "excerpt": chunk.text,
                "relevance_score": sr.score,
                "matched_terms": sr.matched_terms,
                "evidence_id": str(ev.evidence_id),
            })

        return ToolResult(
            success=True,
            data={"query": args.query, "results": excerpts, "total": len(excerpts)},
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # context.retrieve
    # ------------------------------------------------------------------

    def execute_retrieve(self, args: ContextRetrieveInput, call: ToolCall) -> ToolResult:
        """Retrieve relevant context excerpts with EvidenceItem provenance."""
        return self._intelligence.retrieve(
            query=args.query,
            task_id=call.task_id,
            top_k=args.top_k,
            document_ids=args.document_ids,
        )

    # ------------------------------------------------------------------
    # context.relate
    # ------------------------------------------------------------------

    def execute_relate(self, args: ContextRelateInput, call: ToolCall) -> ToolResult:
        """Find inferred relationships across document sources for a topic."""
        return self._intelligence.relate(
            query=args.query,
            task_id=call.task_id,
            top_k=args.top_k,
            document_ids=args.document_ids,
        )

    def register_all(self, registry: Any) -> None:
        """Register all four tools with a ToolRegistry instance."""
        registry.register(DOCUMENT_UPLOAD_DEFINITION, DocumentUploadInput, self.execute_upload)
        registry.register(DOCUMENT_SEARCH_DEFINITION, DocumentSearchInput, self.execute_search)
        registry.register(CONTEXT_RETRIEVE_DEFINITION, ContextRetrieveInput, self.execute_retrieve)
        registry.register(CONTEXT_RELATE_DEFINITION, ContextRelateInput, self.execute_relate)
