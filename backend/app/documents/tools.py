"""Tool definitions and callables for document and context tools.

Serializable ToolDefinition objects are kept separate from executable callables,
per the TOOL_CONTRACT specification. The callable interface is:
    execute(validated_arguments: BaseModel, tool_call: ToolCall) -> ToolResult
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

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

    relative_path: str = Field(
        min_length=1,
        description=(
            "Path relative to UNBOUND_DOCUMENT_IMPORT_DIR. Absolute paths and paths "
            "outside that user-configured directory are rejected."
        ),
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
    # This reads a user-selected local file. Require explicit approval even
    # though the resulting document is stored locally.
    requires_approval=True,
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
        import_dir: Path | str | None = None,
    ) -> None:
        self._store = store
        self._searcher = searcher
        self._intelligence = intelligence
        configured_import_dir = import_dir or os.getenv("UNBOUND_DOCUMENT_IMPORT_DIR")
        self._import_dir = Path(configured_import_dir).expanduser().resolve() if configured_import_dir else None

    @property
    def local_import_enabled(self) -> bool:
        return self._import_dir is not None

    # ------------------------------------------------------------------
    # document.upload
    # ------------------------------------------------------------------

    def execute_upload(self, args: DocumentUploadInput, call: ToolCall) -> ToolResult:
        """
        Ingest a file only from the explicitly configured user import directory.
        Public API uploads use bytes and do not expose this path-based tool surface.
        """
        if self._import_dir is None:
            return ToolResult(success=False, error="Local document import is unavailable; configure UNBOUND_DOCUMENT_IMPORT_DIR")
        relative = Path(args.relative_path)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            return ToolResult(success=False, error="Import path must be relative and remain inside the configured import directory")
        file_path = (self._import_dir / relative).resolve()
        try:
            file_path.relative_to(self._import_dir)
        except ValueError:
            return ToolResult(success=False, error="Import path must remain inside the configured import directory")
        # Validate file type before reading
        original_filename = file_path.name
        file_type = infer_file_type(original_filename)
        if file_type is None:
            return ToolResult(
                success=False,
                error="Unsupported document type. Supported types are PDF, DOCX, and TXT",
            )
        try:
            if not file_path.is_file():
                return ToolResult(success=False, error="Import file was not found in the configured import directory")
            if file_path.stat().st_size > 20 * 1024 * 1024:
                return ToolResult(success=False, error="Document exceeds the 20 MiB upload limit")
            file_bytes = file_path.read_bytes()
        except OSError as exc:
            logger.warning("Document import read failed: exception_class=%s", type(exc).__name__)
            return ToolResult(success=False, error="Could not read the selected document")

        document_id = str(uuid4())
        extraction = extract_document(file_bytes, document_id, original_filename)
        if extraction.status.value == "failed":
            return ToolResult(success=False, error="Document extraction failed; no document was stored")

        try:
            _, stored_filename, stored_path = self._store.save_file(
                file_bytes, original_filename, document_id=document_id
            )
            record = self._store.save_extraction(
                document_id=document_id,
                original_filename=original_filename,
                stored_filename=stored_filename,
                file_path=stored_path,
                result=extraction,
            )
        except Exception as exc:
            logger.warning("Document import persistence failed: exception_class=%s", type(exc).__name__)
            if "stored_filename" in locals():
                self._store.cleanup_failed_ingestion(document_id, stored_filename)
            return ToolResult(success=False, error="Document storage failed; the import was not completed")

        evidence = EvidenceItem(
            task_id=call.task_id,
            source_type=EvidenceSourceType.DOCUMENT,
            source_name=record.original_filename,
            file_path=stored_path,
            excerpt=extraction.full_text[:500] if extraction.full_text else None,
            retrieved_at=utc_now(),
            supports_claim=(
                f"Document '{record.original_filename}' was uploaded, extracted "
                f"(status: {extraction.status.value}), and stored with ID {document_id}."
            ),
        )

        return ToolResult(
            success=extraction.status.value in ("success", "empty"),
            data={
                "document_id": document_id,
                "original_filename": record.original_filename,
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

    def register_all(self, registry_or_runtime: Any, *, allow_upload: bool = True) -> None:
        """Register tool callables, attaching verifiers when given TaskRuntime."""
        entries = [
            (DOCUMENT_UPLOAD_DEFINITION, DocumentUploadInput, self.execute_upload, self.verify_upload),
            (DOCUMENT_SEARCH_DEFINITION, DocumentSearchInput, self.execute_search, self.verify_search),
            (CONTEXT_RETRIEVE_DEFINITION, ContextRetrieveInput, self.execute_retrieve, self.verify_retrieve),
            (CONTEXT_RELATE_DEFINITION, ContextRelateInput, self.execute_relate, self.verify_relate),
        ]
        runtime = registry_or_runtime if callable(getattr(registry_or_runtime, "register_tool", None)) else None
        registry = runtime.registry if runtime is not None else registry_or_runtime
        existing = {tool.tool_name for tool in registry.list_tools()}
        for definition, input_model, execute, verifier in entries:
            if definition.tool_name == "document.upload" and not allow_upload:
                continue
            if definition.tool_name in existing:
                continue
            if runtime is not None:
                runtime.register_tool(definition, input_model, execute, verifier=verifier)
            else:
                registry.register(definition, input_model, execute)

    def verify_upload(self, tool_name: str, expected: str, result: ToolResult) -> bool:
        document_id = (result.data or {}).get("document_id") if isinstance(result.data, dict) else None
        record = self._store.get_record(str(document_id)) if document_id else None
        return bool(
            tool_name == "document.upload" and result.success and record is not None
            and Path(record.file_path).is_file()
            and record.extraction_status.value in {"success", "empty"}
        )

    @staticmethod
    def verify_search(tool_name: str, expected: str, result: ToolResult) -> bool:
        data = result.data if isinstance(result.data, dict) else {}
        results = data.get("results")
        return bool(
            tool_name == "document.search" and result.success and isinstance(results, list)
            and data.get("total") == len(results) and len(results) > 0
            and len(result.evidence) == len(results)
        )

    @staticmethod
    def verify_retrieve(tool_name: str, expected: str, result: ToolResult) -> bool:
        data = result.data if isinstance(result.data, dict) else {}
        results = data.get("results")
        return bool(
            tool_name == "context.retrieve" and result.success and isinstance(results, list)
            and len(results) > 0 and len(result.evidence) > 0
        )

    @staticmethod
    def verify_relate(tool_name: str, expected: str, result: ToolResult) -> bool:
        data = result.data if isinstance(result.data, dict) else {}
        return bool(
            tool_name == "context.relate" and result.success
            and isinstance(data.get("relationships"), list)
            and data.get("sources_analyzed", 0) > 0 and len(result.evidence) > 0
        )
