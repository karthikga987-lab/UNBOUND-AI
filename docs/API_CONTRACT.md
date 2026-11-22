# Initial API Contract

This document describes intended endpoint interfaces. **No API routes or application entry point exist in the inspected repository yet.** These endpoints are planned and must not be treated as implemented until corresponding route code is added and verified. All JSON models below use Pydantic 2 shared contracts where a model exists.

Common error body (proposed): `{"detail": "human-readable explanation", "code": "stable_error_code"}`. Implementations may add request IDs, but should not expose credentials or internal stack traces.

## Health

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `GET /api/health` | Report service health. | None | Not yet modeled; proposed `{"status":"ok"}`. | `503` if unavailable. No approval. |

## Tasks

| Method / path | Purpose | Request | Response | Errors / approval |
|---|---|---|---|---|
| `POST /api/tasks` | Create a task from natural language and request execution preference. | `TaskCreateRequest` (`user_request`, `auto_execute=false` by default). | `TaskResponse` | `422` invalid request; `503` required LLM unavailable. Creating a task does not waive approvals. |
| `GET /api/tasks/{task_id}` | Retrieve task state, plan, approvals, evidence, artifacts, and result. | UUID path parameter | `TaskResponse` | `404` unknown task. No approval. |
| `POST /api/tasks/{task_id}/execute` | Start or continue eligible task steps. | No body defined in the initial contracts. | `TaskResponse` | `404` unknown task; `409` invalid state; `422` invalid transition. Mandatory step approvals remain required, including with `auto_execute=true`. |
| `POST /api/tasks/{task_id}/cancel` | Request cancellation. | No body defined. | `TaskResponse` | `404` unknown task; `409` already terminal, if implementation chooses to reject it. No approval. |
| `GET /api/tasks/{task_id}/events` | Retrieve ordered execution events. | UUID path parameter | `list[ExecutionEvent]` | `404` unknown task. No approval. |

Approval decision API shape is not included in the initial endpoint list; the task response exposes `pending_approvals: list[ApprovalRequest]`. The team must agree on an approval endpoint before implementing UI/API approval submission.

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
