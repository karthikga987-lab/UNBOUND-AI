"""MCP server for UNBOUND AI document and context intelligence.

Exposes read-only document search, context retrieval, relationships, and evidence
via the official Python MCP SDK. Upload is deliberately omitted from the standalone
MCP surface because that server has no approval manager; ingestion uses the
user-initiated FastAPI upload route or approval-gated internal tool.

Tool naming follows the agreed TOOL_CONTRACT namespace:
- document_search   (maps to document.search)
- context_retrieve  (maps to context.retrieve)
- context_relate    (maps to context.relate)
- evidence_list     (retrieval of stored evidence)

MCP tool names use underscores (MCP convention); the UNBOUND tool names
use dots (orchestrator registry convention). The adapter maps between them.

This module is modular: DocumentMCPServer is constructed with concrete
service instances and returns an mcp.Server that can be run standalone
or embedded.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from backend.app.documents.extractor import extract_document, infer_file_type
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.context import ContextIntelligence
from backend.app.documents.tools import ContextRelateInput, ContextRetrieveInput, DocumentSearchInput
from backend.app.schemas.contracts import EvidenceSourceType, utc_now

logger = logging.getLogger(__name__)


class EvidenceListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str | None = Field(default=None, min_length=1)


class DocumentMCPServer:
    """
    Minimal MCP server wrapping document and context intelligence services.

    Builds an mcp.Server and registers tools using the official MCP Python SDK.
    If the SDK is not installed the server cannot start, but the tool
    implementations are still importable for testing.
    """

    def __init__(
        self,
        store: DocumentStore,
        searcher: DocumentSearcher,
        intelligence: ContextIntelligence,
        server_name: str = "unbound-document-context",
        server_version: str = "0.1.0",
        import_dir: Path | str | None = None,
    ) -> None:
        self._store = store
        self._searcher = searcher
        self._intelligence = intelligence
        self._server_name = server_name
        self._server_version = server_version
        configured_import_dir = import_dir or os.getenv("UNBOUND_DOCUMENT_IMPORT_DIR")
        self._import_dir = Path(configured_import_dir).expanduser().resolve() if configured_import_dir else None

    # ------------------------------------------------------------------
    # Tool implementations (pure Python, testable without MCP runtime)
    # ------------------------------------------------------------------

    def tool_document_upload(
        self, relative_path: str, original_filename: str, *, user_approved: bool = False
    ) -> dict[str, Any]:
        """Read an approved file only from the configured import directory."""
        if not user_approved:
            return {"success": False, "error": "Explicit user approval is required for local document import"}
        if self._import_dir is None:
            return {"success": False, "error": "Local import is unavailable; configure UNBOUND_DOCUMENT_IMPORT_DIR"}
        rel = Path(relative_path)
        if rel.is_absolute() or rel.drive or ".." in rel.parts:
            return {"success": False, "error": "Import path must remain inside the configured import directory"}
        fp = (self._import_dir / rel).resolve()
        try:
            fp.relative_to(self._import_dir)
        except ValueError:
            return {"success": False, "error": "Import path must remain inside the configured import directory"}
        try:
            if not fp.is_file() or fp.stat().st_size > 20 * 1024 * 1024:
                return {"success": False, "error": "Import file is unavailable or exceeds the 20 MiB limit"}
        except OSError as exc:
            logger.warning("MCP document stat failed: exception_class=%s", type(exc).__name__)
            return {"success": False, "error": "Import file is unavailable or exceeds the 20 MiB limit"}

        original_filename = fp.name
        file_type = infer_file_type(original_filename)
        if file_type is None:
            return {
                "success": False,
                "error": f"Unsupported file type for '{original_filename}'",
            }

        try:
            file_bytes = fp.read_bytes()
        except Exception as exc:
            logger.warning("MCP document read failed: exception_class=%s", type(exc).__name__)
            return {"success": False, "error": "Could not read the selected document"}

        document_id = str(uuid.uuid4())
        extraction = extract_document(file_bytes, document_id, original_filename)
        if extraction.status.value == "failed":
            return {"success": False, "error": "Document extraction failed; no document was stored"}
        try:
            document_id, stored_filename, stored_path = self._store.save_file(
                file_bytes, original_filename, document_id=document_id
            )
        except Exception as exc:
            logger.warning("MCP document storage failed: exception_class=%s", type(exc).__name__)
            return {"success": False, "error": "Document storage failed"}

        try:
            self._store.save_extraction(
                document_id=document_id,
                original_filename=original_filename,
                stored_filename=stored_filename,
                file_path=stored_path,
                result=extraction,
            )
        except Exception as exc:
            logger.warning("MCP document persistence failed: exception_class=%s", type(exc).__name__)
            self._store.cleanup_failed_ingestion(document_id, stored_filename)
            return {"success": False, "error": "Document ingestion persistence failed"}

        return {
            "success": extraction.status.value in ("success", "empty"),
            "document_id": document_id,
            "original_filename": original_filename,
            "file_type": extraction.file_type.value,
            "extraction_status": extraction.status.value,
            "page_count": extraction.page_count,
            "chunk_count": len(extraction.chunks),
            "error": extraction.error,
        }

    def tool_document_search(
        self,
        query: str,
        top_k: int = 5,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search indexed document content. Returns ranked excerpts."""
        results = self._searcher.search(query, top_k=top_k, document_ids=document_ids)
        excerpts = []
        for sr in results:
            excerpts.append({
                "document_id": sr.document_record.document_id,
                "filename": sr.document_record.original_filename,
                "file_type": sr.document_record.file_type.value,
                "page": sr.chunk.page,
                "section": sr.chunk.section,
                "excerpt": sr.chunk.text,
                "relevance_score": sr.score,
                "matched_terms": sr.matched_terms,
            })
        return {"query": query, "results": excerpts, "total": len(excerpts)}

    def tool_context_retrieve(
        self,
        query: str,
        task_id: str | None = None,
        top_k: int = 5,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve relevant excerpts with EvidenceItem provenance."""
        tid = uuid.UUID(task_id) if task_id else uuid.uuid4()
        result = self._intelligence.retrieve(
            query=query, task_id=tid, top_k=top_k, document_ids=document_ids
        )
        return {
            "success": result.success,
            "data": result.data,
            "evidence": [ev.model_dump(mode="json") for ev in result.evidence],
            "error": result.error,
        }

    def tool_context_relate(
        self,
        query: str,
        task_id: str | None = None,
        top_k: int = 10,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find inferred relationships across sources. Results are labelled as inferred."""
        tid = uuid.UUID(task_id) if task_id else uuid.uuid4()
        result = self._intelligence.relate(
            query=query, task_id=tid, top_k=top_k, document_ids=document_ids
        )
        return {
            "success": result.success,
            "data": result.data,
            "evidence": [ev.model_dump(mode="json") for ev in result.evidence],
            "error": result.error,
        }

    def tool_evidence_list(self, document_id: str | None = None) -> dict[str, Any]:
        """List stored documents (and optionally their chunks) as an evidence manifest."""
        if document_id:
            record = self._store.get_record(document_id)
            if record is None:
                return {"success": False, "error": f"Document '{document_id}' not found"}
            chunks = self._store.get_chunks(document_id)
            return {
                "success": True,
                "document": record.model_dump(mode="json"),
                "chunk_count": len(chunks),
            }
        records = self._store.list_records()
        return {
            "success": True,
            "documents": [r.model_dump(mode="json") for r in records],
            "total": len(records),
        }

    # ------------------------------------------------------------------
    # MCP server construction
    # ------------------------------------------------------------------

    def build_server(self):
        """
        Construct and return a configured mcp.Server instance.

        Requires the `mcp` package (official Python MCP SDK).
        Tools are registered using the SDK's @server.call_tool decorator pattern
        via the low-level Server API for compatibility.
        """
        try:
            from mcp.server import Server
            from mcp.server.models import InitializationOptions
            from mcp.types import (
                CallToolRequest,
                CallToolResult,
                TextContent,
                Tool,
            )
        except ImportError as exc:
            raise ImportError(
                "MCP SDK not installed. Run: pip install mcp"
            ) from exc

        server = Server(self._server_name)

        # Register tool listing handler
        @server.list_tools()
        async def list_tools():
            return [
                Tool(
                    name="document_search",
                    description="Search indexed document content. Returns ranked excerpts with source metadata.",
                    inputSchema=DocumentSearchInput.model_json_schema(),
                ),
                Tool(
                    name="context_retrieve",
                    description=(
                        "Retrieve relevant document excerpts with EvidenceItem provenance "
                        "traceable to source documents."
                    ),
                    inputSchema=ContextRetrieveInput.model_json_schema(),
                ),
                Tool(
                    name="context_relate",
                    description=(
                        "Find shared entities and inferred structural patterns across document sources. "
                        "Results are explicitly labelled as inferred."
                    ),
                    inputSchema=ContextRelateInput.model_json_schema(),
                ),
                Tool(
                    name="evidence_list",
                    description="List stored documents and their extraction status.",
                    inputSchema=EvidenceListInput.model_json_schema(),
                ),
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list:
            try:
                if name == "document_search":
                    parsed = DocumentSearchInput.model_validate(arguments)
                    result = self.tool_document_search(
                        query=parsed.query,
                        top_k=parsed.top_k,
                        document_ids=parsed.document_ids,
                    )
                elif name == "context_retrieve":
                    parsed = ContextRetrieveInput.model_validate(arguments)
                    result = self.tool_context_retrieve(
                        query=parsed.query,
                        top_k=parsed.top_k,
                        document_ids=parsed.document_ids,
                    )
                elif name == "context_relate":
                    parsed = ContextRelateInput.model_validate(arguments)
                    result = self.tool_context_relate(
                        query=parsed.query,
                        top_k=parsed.top_k,
                        document_ids=parsed.document_ids,
                    )
                elif name == "evidence_list":
                    parsed = EvidenceListInput.model_validate(arguments)
                    result = self.tool_evidence_list(
                        document_id=parsed.document_id,
                    )
                else:
                    result = {"error": f"Unknown tool: {name}"}
            except Exception as exc:
                logger.warning("MCP tool rejected: tool_name=%s exception_class=%s", name, type(exc).__name__)
                result = {"error": "Invalid tool input or tool execution failed"}

            return [TextContent(type="text", text=json.dumps(result, default=str, indent=2))]

        return server
