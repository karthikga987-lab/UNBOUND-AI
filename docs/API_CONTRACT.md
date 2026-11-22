# UNBOUND AI API Contract

The task and health endpoints listed below are implemented by `backend/app/main.py`. Browser, document, and integrations endpoints remain future interfaces and are not currently exposed. All JSON task models use the shared Pydantic 2 contracts.

Errors use `{"detail":{"code":"stable_error_code","message":"human-readable explanation"}}`. Responses must not expose credentials or internal stack traces.

## Health

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `GET /api/health` | Report service health without exposing credentials. | None | `{"status":"ok","llm_configured":boolean,"tools_registered":boolean,"registered_tools_count":integer}` | `200`, including when no LLM is configured. No approval. |

## Tasks

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `POST /api/tasks` | Create a task from natural language and request execution preference. | `TaskCreateRequest` (`user_request`, `auto_execute=false` by default). | `TaskResponse` | `422` invalid request; `503` required LLM unavailable. Creating a task does not waive approvals. |
| `GET /api/tasks/{task_id}` | Retrieve task state, plan, approvals, evidence, artifacts, and result. | UUID path parameter | `TaskResponse` | `404` unknown task. No approval. |
| `POST /api/tasks/{task_id}/execute` | Start or continue eligible task steps. | No body. | `TaskResponse` | `404` unknown task; state outcomes, including awaiting approval and verification attention, are returned in `TaskResponse`. Mandatory approvals remain required. |
| `POST /api/tasks/{task_id}/cancel` | Request cancellation. | No body. | `TaskResponse` | `404` unknown task. Cancellation of terminal tasks is idempotent. No approval. |
| `GET /api/tasks/{task_id}/events` | Retrieve ordered execution events. | UUID path parameter | `list[ExecutionEvent]` | `404` unknown task. No approval. |
| `POST /api/tasks/{task_id}/approvals/{approval_id}` | Resolve one pending approval. | `{"decision":"APPROVED"}` or `{"decision":"REJECTED"}` | Updated `TaskResponse` | `404` unknown task or pending approval; `422` invalid decision. Approval does not start execution; call the execute endpoint explicitly. |

Task creation invokes the configured LLM to understand intent and generate a plan. Missing LLM configuration or provider failure returns `503`. `auto_execute` defaults to false; when true, the existing executor is invoked and still enforces approval and verification. If no verifier exists, the executor leaves the task needing attention without invoking a tool.

Errors use a `detail` object with a stable `code` and safe `message`. Invalid request bodies and path UUIDs return `422`; unknown tasks return `404`. Unexpected errors return a generic `500` response without a traceback.

## Backend startup

From the repository root, in PowerShell:

```powershell
cd backend
$env:PYTHONPATH = (Resolve-Path ..).Path
python -m uvicorn app.main:app --reload
```

`PYTHONPATH` includes the repository root because the existing orchestration modules import through the `backend.app` package.

The API base URL is `http://127.0.0.1:8000/api`; interactive Swagger documentation is at `http://127.0.0.1:8000/docs`.

LLM configuration is optional for application startup and required for task creation. Supported environment variables are `LLM_PROVIDER`, `LLM_MODEL`, optional `LLM_BASE_URL`, `OPENAI_API_KEY` or `LLM_API_KEY` for cloud providers, and `LLM_TIMEOUT_SECONDS`. Ollama uses `LLM_PROVIDER=ollama` and defaults to `http://localhost:11434/v1`. Credentials are never returned by the API.

Task and approval state currently lives in process memory and is lost when the application restarts. The default runtime has no registered browser, document, context, or report tools; integrations must register real implementations before those capabilities are available.

External actions already started by a registered callable may not be reversible. Cancellation prevents later steps, but cannot guarantee rollback of an in-flight browser or remote operation.

## Browser

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `POST /api/browser/open` | Open a browser target for controlled interaction. | No shared request model yet. | No shared response model yet. | `422` invalid URL; `502` browser failure. Navigation itself requires no approval; consequential actions do. |
| `POST /api/browser/record/start` | Start recording a reusable workflow. | No shared request model yet. | No shared response model yet. | `409` recording conflict; `502` browser failure. No approval for recording. |
| `POST /api/browser/record/stop` | Stop recording and save the workflow. | No shared request model yet. | `WorkflowDefinition` (planned response). | `409` no active recording; `422` invalid workflow. No approval for saving. |
| `GET /api/browser/workflows` | List saved workflows. | None | `list[WorkflowDefinition]` (planned response). | `503` storage unavailable. No approval. |
| `POST /api/browser/replay/{workflow_id}` | Replay a saved workflow, validating parameters and verifying actions. | UUID path; workflow parameter body shape not yet modeled. | `TaskResponse` (planned). | `404` workflow not found; `422` invalid parameters; `409` workflow not replayable. Sensitive steps require explicit approval. |

## Documents

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `POST /api/documents/upload` | Upload and ingest a supported document. | Multipart file; no shared request model yet. | No shared document response model yet. | `413` too large; `415` unsupported type; `422` invalid content; `503` storage/ingestion unavailable. No approval. |
| `GET /api/documents` | List available documents. | Optional query parameters not yet modeled. | No shared document response model yet. | `503` storage unavailable. No approval. |
| `POST /api/documents/search` | Search indexed document content. | No shared search request model yet. | No shared search response model yet; results must retain `EvidenceItem` provenance. | `422` invalid query; `503` retrieval unavailable. Read-only retrieval requires no approval. |

## Integrations

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `GET /api/integrations` | List configured integrations and availability without exposing secrets. | None | No shared integration response model yet. | `503` integration state unavailable. No approval. |

The team should add request/response contracts before implementing the browser, document, approval, and integration endpoints. Do not create placeholder routes to match this document.
