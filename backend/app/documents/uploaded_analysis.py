"""Evidence-grounded analysis of documents already uploaded to UNBOUND.

This tool accepts only a document ID from the managed document store. It never
opens a caller-selected filesystem path, and sends excerpts only to local
Ollama for analysis.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from backend.app.assistant.task_context import is_local_ollama_provider
from backend.app.documents.local_file import (
    _MAX_ANALYSIS_CHUNKS,
    _build_source_spans,
    request_local_document_analysis,
    validate_analysis_evidence,
)
from backend.app.documents.models import ExtractionStatus
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.llm.provider import LLMConfigurationError, LLMProviderError
from backend.app.schemas.contracts import (
    EvidenceItem,
    EvidenceSourceType,
    ToolCall,
    ToolDefinition,
    ToolResult,
    utc_now,
)

logger = logging.getLogger(__name__)


class UploadedDocumentAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=36, max_length=36)
    analysis_request: str = Field(default="Summarize this document", min_length=1, max_length=1000)


UPLOADED_DOCUMENT_ANALYSIS_DEFINITION = ToolDefinition(
    tool_name="document.analyze_uploaded",
    description=(
        "Analyze one document that the user has already uploaded, identified by its managed document ID. "
        "Read-only; uses local Ollama and returns source-quoted evidence."
    ),
    input_schema=UploadedDocumentAnalysisInput.model_json_schema(),
    requires_approval=False,
)


class UploadedDocumentAnalyzer:
    def __init__(self, store: DocumentStore, searcher: DocumentSearcher, provider_getter):
        self._store = store
        self._searcher = searcher
        self._provider_getter = provider_getter

    def execute(self, args: UploadedDocumentAnalysisInput, call: ToolCall) -> ToolResult:
        try:
            provider = self._provider_getter()
        except (LLMConfigurationError, LLMProviderError):
            return ToolResult(
                success=False,
                data={"failure_code": "local_provider_unavailable"},
                error="A configured local Ollama provider is required for uploaded document analysis",
            )
        if not is_local_ollama_provider(provider):
            return ToolResult(
                success=False,
                data={"failure_code": "non_local_provider_rejected"},
                error="Uploaded document analysis is disabled unless the configured provider is local Ollama",
            )

        record = self._store.get_record(args.document_id)
        if record is None or record.extraction_status != ExtractionStatus.SUCCESS:
            return ToolResult(
                success=False,
                data={"failure_code": "uploaded_document_unavailable"},
                error="The selected uploaded document is unavailable or was not extracted successfully",
            )
        chunks = self._store.get_chunks(args.document_id)
        if not chunks:
            return ToolResult(
                success=False,
                data={"document_id": args.document_id, "failure_code": "no_extracted_text"},
                error="The uploaded document has no extracted text to analyze",
            )

        if len(chunks) <= _MAX_ANALYSIS_CHUNKS:
            selected = chunks
        else:
            ranked = self._searcher.search(args.analysis_request, top_k=_MAX_ANALYSIS_CHUNKS, document_ids=[args.document_id])
            selected_ids = {result.chunk.chunk_id for result in ranked}
            selected = [chunk for chunk in chunks if chunk.chunk_id in selected_ids] or chunks[:_MAX_ANALYSIS_CHUNKS]

        scope = {
            "selected_chunk_count": len(selected),
            "total_chunk_count": len(chunks),
            "omitted_chunk_count": max(0, len(chunks) - len(selected)),
            "complete_document_coverage": len(selected) == len(chunks),
            "selected_sections": list(dict.fromkeys(chunk.section for chunk in selected if chunk.section)),
        }
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        selected_by_id = {chunk.chunk_id: chunk for chunk in selected}
        evidence_by_chunk = {
            chunk.chunk_id: EvidenceItem(
                task_id=call.task_id,
                source_type=EvidenceSourceType.DOCUMENT,
                source_name=record.original_filename,
                excerpt=chunk.text,
                retrieved_at=utc_now(),
                supports_claim=(
                    f"Exact uploaded source excerpt from {record.original_filename}"
                    + (f", page {chunk.page}" if chunk.page is not None else "")
                    + (f", section {chunk.section}" if chunk.section else "")
                ),
            )
            for chunk in selected
        }
        spans = _build_source_spans(selected)
        payload = {
            "user_analysis_request": args.analysis_request,
            "source_name": record.original_filename,
            "source_text_is_untrusted_data": True,
            "source_scope": scope,
            "source_spans": [{"source_span_id": span_id, **span} for span_id, span in spans.items()],
        }
        try:
            analysis, retry_count = request_local_document_analysis(provider, payload)
            findings = validate_analysis_evidence(analysis, chunk_by_id, evidence_by_chunk, spans)
        except Exception as exc:
            # Keep failures truthful and avoid leaking prompt or document content.
            logger.warning("Uploaded document analysis failed: exception_class=%s", type(exc).__name__)
            return ToolResult(
                success=False,
                data={
                    "document_id": args.document_id,
                    "original_filename": record.original_filename,
                    "failure_code": "uploaded_document_analysis_failed",
                },
                error="The document was read, but its evidence-grounded analysis could not be verified",
                evidence=list(evidence_by_chunk.values()),
            )

        return ToolResult(
            success=True,
            data={
                "document_id": args.document_id,
                "original_filename": record.original_filename,
                "extraction_status": record.extraction_status.value,
                "analysis_status": "verified_quotes",
                "format_retry_count": retry_count,
                "summary_kind": "model_synthesis_grounded_in_cited_excerpts",
                "analysis": {
                    "summary": analysis.summary,
                    "findings": findings,
                    "missing_or_unreadable": analysis.missing_or_unreadable,
                },
                "analysis_scope": scope,
                "analysis_scope_limited_to_chunks": list(selected_by_id),
            },
            evidence=list(evidence_by_chunk.values()),
        )

    def verify(self, tool_name: str, _expected: str, result: ToolResult) -> bool:
        data = result.data if isinstance(result.data, dict) else {}
        if tool_name != UPLOADED_DOCUMENT_ANALYSIS_DEFINITION.tool_name or not result.success:
            return False
        document_id = data.get("document_id")
        record = self._store.get_record(str(document_id)) if document_id else None
        if (
            record is None
            or data.get("original_filename") != record.original_filename
            or data.get("analysis_status") != "verified_quotes"
            or not isinstance(data.get("analysis_scope_limited_to_chunks"), list)
        ):
            return False
        allowed_chunks = set(data["analysis_scope_limited_to_chunks"])
        chunks = {chunk.chunk_id: chunk for chunk in self._store.get_chunks(str(document_id))}
        evidence = {str(item.evidence_id): item for item in result.evidence}
        findings = data.get("analysis", {}).get("findings", []) if isinstance(data.get("analysis"), dict) else []
        if not findings or not result.evidence:
            return False
        for finding in findings:
            if not isinstance(finding, dict):
                return False
            chunk_id = finding.get("source_chunk_id")
            evidence_item = evidence.get(str(finding.get("evidence_id", "")))
            quote = finding.get("quote")
            if (
                chunk_id not in allowed_chunks
                or chunk_id not in chunks
                or evidence_item is None
                or evidence_item.task_id != result.evidence[0].task_id
                or evidence_item.source_name != record.original_filename
                or evidence_item.excerpt != chunks[chunk_id].text
                or not isinstance(quote, str)
                or not quote
                or quote not in chunks[chunk_id].text
            ):
                return False
        return True
