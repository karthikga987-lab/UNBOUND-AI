"""MCP server for UNBOUND AI document and context intelligence.

Exposes document upload, search, context retrieval, and evidence retrieval
via the official Python MCP SDK.

Tool naming follows the agreed TOOL_CONTRACT namespace:
- document_upload   (maps to document.upload)
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
import uuid
from pathlib import Path
from typing import Any

from backend.app.documents.extractor import extract_document, infer_file_type
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.intelligence.context import ContextIntelligence
from backend.app.schemas.contracts import EvidenceSourceType, utc_now

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._store = store
        self._searcher = searcher
        self._intelligence = intelligence
        self._server_name = server_name
        self._server_version = server_version

    # ------------------------------------------------------------------
    # Tool implementations (pure Python, testable without MCP runtime)
    # ------------------------------------------------------------------

    def tool_document_upload(self, file_path: str, original_filename: str) -> dict[str, Any]:
        """Upload and ingest a document. Returns document_id and extraction status."""
        fp = Path(file_path)
        if not fp.exists():
            return {"success": False, "error": f"File not found: {file_path}"}

        file_type = infer_file_type(original_filename)
        if file_type is None:
            return {
                "success": False,
                "error": f"Unsupported file type for '{original_filename}'",
            }

        try:
            file_bytes = fp.read_bytes()
        except Exception as exc:
            return {"success": False, "error": f"Cannot read file: {exc}"}

        try:
            document_id, stored_filename, stored_path = self._store.save_file(
                file_bytes, original_filename
            )
        except Exception as exc:
            return {"success": False, "error": f"Storage error: {exc}"}

        extraction = extract_document(file_bytes, document_id, original_filename)

        try:
            self._store.save_extraction(
                document_id=document_id,
                original_filename=original_filename,
                stored_filename=stored_filename,
                file_path=stored_path,
                result=extraction,
            )
        except Exception as exc:
            return {"success": False, "error": f"Persistence error: {exc}"}

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
                    name="document_upload",
                    description=(
                        "Upload and ingest a document (PDF, DOCX, TXT) from a file path. "
                        "Returns document_id and extraction status."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path to the file"},
                            "original_filename": {"type": "string", "description": "Original filename with extension"},
                        },
                        "required": ["file_path", "original_filename"],
                    },
                ),
                Tool(
                    name="document_search",
                    description="Search indexed document content. Returns ranked excerpts with source metadata.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                            "document_ids": {
                                "type": "array", "items": {"type": "string"},
                                "description": "Optional: restrict to these document IDs",
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="context_retrieve",
                    description=(
                        "Retrieve relevant document excerpts with EvidenceItem provenance "
                        "traceable to source documents."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "task_id": {"type": "string", "description": "Optional UUID for evidence linkage"},
                            "top_k": {"type": "integer", "default": 5},
                            "document_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="context_relate",
                    description=(
                        "Find shared entities and inferred structural patterns across document sources. "
                        "Results are explicitly labelled as inferred."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "task_id": {"type": "string"},
                            "top_k": {"type": "integer", "default": 10},
                            "document_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="evidence_list",
                    description="List stored documents and their extraction status.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "document_id": {
                                "type": "string",
                                "description": "Optional: retrieve detail for a specific document",
                            },
                        },
                    },
                ),
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list:
            try:
                if name == "document_upload":
                    result = self.tool_document_upload(
                        file_path=arguments["file_path"],
                        original_filename=arguments["original_filename"],
                    )
                elif name == "document_search":
                    result = self.tool_document_search(
                        query=arguments["query"],
                        top_k=arguments.get("top_k", 5),
                        document_ids=arguments.get("document_ids"),
                    )
                elif name == "context_retrieve":
                    result = self.tool_context_retrieve(
                        query=arguments["query"],
                        task_id=arguments.get("task_id"),
                        top_k=arguments.get("top_k", 5),
                        document_ids=arguments.get("document_ids"),
                    )
                elif name == "context_relate":
                    result = self.tool_context_relate(
                        query=arguments["query"],
                        task_id=arguments.get("task_id"),
                        top_k=arguments.get("top_k", 10),
                        document_ids=arguments.get("document_ids"),
                    )
                elif name == "evidence_list":
                    result = self.tool_evidence_list(
                        document_id=arguments.get("document_id"),
                    )
                else:
                    result = {"error": f"Unknown tool: {name}"}
            except Exception as exc:
                logger.exception("MCP tool '%s' raised an unexpected error", name)
                result = {"error": f"Tool execution error: {exc}"}

            return [TextContent(type="text", text=json.dumps(result, default=str, indent=2))]

        return server
