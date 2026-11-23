from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backend.app.api.runtime import TaskRuntime
from backend.app.documents.extractor import extract_document
from backend.app.documents.store import DocumentStore
from backend.app.documents.tools import DocumentUploadInput
from backend.app.schemas.contracts import (
    StepStatus,
    TaskCreateRequest,
    TaskStatus,
    TaskStep,
    ToolCall,
)


def _store_document(store: DocumentStore, filename: str, content: bytes) -> str:
    document_id, stored_filename, stored_path = store.save_file(content, filename)
    extraction = extract_document(content, document_id, filename)
    store.save_extraction(document_id, filename, stored_filename, stored_path, extraction)
    return document_id


def test_runtime_registers_document_context_tools_with_verifiers_and_shared_services(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    runtime = TaskRuntime(document_store=store)
    runtime.register_document_tools()

    assert runtime.document_searcher._store is store
    assert runtime.context_intelligence._store is store
    assert runtime.document_tools._store is store
    names = {item.tool_name for item in runtime.registry.list_tools()}
    assert {"document.search", "context.retrieve", "context.relate"} <= names
    assert "document.upload" not in names
    assert all(runtime.executor.verifier_for(name) is not None for name in names)


def test_registered_document_search_executes_and_verifies_with_task_owned_evidence(tmp_path):
    store = DocumentStore(tmp_path / "docs")
    _store_document(store, "report.txt", b"Synthetic report: revenue increased by 12 percent in 2025.")
    runtime = TaskRuntime(document_store=store)
    runtime.register_document_tools()
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Find the synthetic revenue evidence"),
        [TaskStep(
            step_id="search_report", tool_name="document.search",
            description="Search the uploaded report", input={"query": "revenue increased"},
            expected_outcome="Return a matching source excerpt with provenance",
        )],
    )

    result = runtime.executor.run(task.task_id)

    assert result.status == TaskStatus.COMPLETED
    assert result.plan[0].status == StepStatus.COMPLETED
    assert result.evidence
    assert all(item.task_id == result.task_id for item in result.evidence)
    assert result.evidence[0].source_name == "report.txt"


def test_local_document_import_is_opt_in_approval_gated_and_confined(tmp_path, monkeypatch):
    import_dir = tmp_path / "approved-import"
    import_dir.mkdir()
    (import_dir / "notes.txt").write_text("Synthetic local note", encoding="utf-8")
    monkeypatch.setenv("UNBOUND_DOCUMENT_IMPORT_DIR", str(import_dir))
    store = DocumentStore(tmp_path / "docs")
    runtime = TaskRuntime(document_store=store)
    runtime.register_document_tools()

    definition = runtime.registry.get_tool("document.upload")
    assert definition.requires_approval is True
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Import notes.txt"),
        [TaskStep(
            step_id="import_notes", tool_name="document.upload",
            description="Import the user-selected local notes file",
            input={"relative_path": "notes.txt"},
            expected_outcome="The selected document is stored and extracted",
        )],
    )
    waiting = runtime.executor.run(task.task_id)
    assert waiting.status == TaskStatus.AWAITING_APPROVAL
    assert store.list_records() == []

    call = ToolCall(task_id=uuid4(), step_id="test", tool_name="document.upload", arguments={})
    traversal = runtime.document_tools.execute_upload(
        DocumentUploadInput(relative_path="../outside.txt"), call
    )
    absolute = runtime.document_tools.execute_upload(
        DocumentUploadInput(relative_path=str(import_dir / "notes.txt")), call
    )
    assert traversal.success is False
    assert absolute.success is False
    assert store.list_records() == []


def test_standalone_mcp_server_initializes_without_network_client(tmp_path):
    pytest.importorskip("mcp")
    from backend.app.documents.search import DocumentSearcher
    from backend.app.intelligence.context import ContextIntelligence
    from backend.app.mcp.server import DocumentMCPServer

    store = DocumentStore(tmp_path / "docs")
    searcher = DocumentSearcher(store)
    intelligence = ContextIntelligence(store, searcher)
    server = DocumentMCPServer(store, searcher, intelligence).build_server()
    assert server is not None
