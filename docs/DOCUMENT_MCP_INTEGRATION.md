# Document and MCP Integration

## Current implementation

The existing FastAPI application owns a single `TaskRuntime`. The default app initializes one `DocumentStore`, `DocumentSearcher`, `ContextIntelligence`, and `DocumentContextTools`, then registers the read-only tools `document.search`, `context.retrieve`, and `context.relate` in the existing `ToolRegistry`. Each receives a tool-specific verifier. There is no second registry or separate document database.

`POST /api/documents/upload` accepts multipart file bytes for PDF, DOCX, or TXT, enforces a 20 MiB file limit, extracts before persistence, and removes stored files if persistence fails. It never accepts a filesystem path. Extraction content is treated as untrusted text; scripts and macros are not executed. Spreadsheet ingestion and semantic/vector search are not implemented.

The document store defaults to `%LOCALAPPDATA%\UNBOUND\documents` on Windows. Set `UNBOUND_DOCUMENT_STORAGE` to select a different local directory; both FastAPI and the standalone MCP server use this setting. The directory contains `documents.db`, `files/`, and `texts/`. It is separate from conversation, memory, reminder, and in-memory task state. Existing databases are not migrated, reset, or overwritten.

## Optional import tool

The orchestrator does not expose `document.upload` unless `UNBOUND_DOCUMENT_IMPORT_DIR` is set before creating the runtime. If enabled, its Pydantic input accepts only a relative path under that configured directory, resolves symlinks before checking the boundary, enforces the file size/type limits, and requires task approval through `ToolDefinition.requires_approval`. Do not point the directory at a drive root, profile root, or broad user folder. Prefer the bytes-only FastAPI upload for ordinary use.

When constructing an injected app runtime, register document adapters explicitly before serving requests:

```python
from backend.app.api.runtime import TaskRuntime
from backend.app.main import create_app

runtime = TaskRuntime()
runtime.register_document_tools()
app = create_app(runtime)
```

Set `UNBOUND_DOCUMENT_IMPORT_DIR` before constructing `TaskRuntime` if the approval-gated import tool is needed. `TaskRuntime.register_tool(definition, input_model, execute, verifier=...)` remains the registration boundary. Runtime callables receive `(validated_arguments, tool_call)`; evidence must use `tool_call.task_id`. Missing verifiers prevent execution, and successful tool calls are still independently verified.

## MCP

Member 4 supplied a standalone server using the official Python MCP SDK and stdio transport. It is not an MCP client: UNBOUND does not connect to external servers or forward user data to them. The server is not started by FastAPI and must be launched explicitly. It exposes read-only document search, context retrieval/relationship, and evidence-list tools. Upload is omitted from the standalone server because it has no task approval manager. The internal orchestrator uses direct adapters to the shared services rather than trusting arbitrary MCP-discovered tools.

Install the API and document dependencies from the repository root:

```powershell
python -m pip install -r backend/requirements.txt
```

Install the MCP SDK only if using the standalone server:

```powershell
python -m pip install -r backend/requirements-mcp.txt
$env:UNBOUND_DOCUMENT_STORAGE = Join-Path $env:LOCALAPPDATA 'UNBOUND\documents'
python -m backend.app.mcp.run
```

Run the API from the repository root in another terminal:

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --reload
```

Swagger is available at `http://127.0.0.1:8000/docs`. Keep the backend loopback-only. SQLite is not encrypted at rest; use the Windows account ACL/device encryption and do not store sensitive secrets in documents.
