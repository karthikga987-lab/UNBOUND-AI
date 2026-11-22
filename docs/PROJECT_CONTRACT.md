# UNBOUND AI Project Contract

## Product vision

UNBOUND AI is a general-purpose digital agent that learns reusable browser workflows, connects information across websites and documents, understands natural-language requests, selects registered tools, executes multi-step tasks, verifies outcomes, and returns evidence-backed results. The product must remain domain-neutral.

## System architecture

The backend is organized around shared Pydantic contracts (`backend/app/schemas/contracts.py`) and implementation modules with one-way dependencies:

- API layer (`backend/app/main.py` and `backend/app/api/`): validates requests and returns shared response models; task state is currently in memory.
- Orchestrator and LLM: interpret requests, plan only with registered tools, manage task state, approvals, execution, recovery, and synthesis.
- Browser intelligence: browser control, recording, learned workflow storage/replay, locator fallback, verification, and downloads.
- MCP/documents/context: MCP integrations, PDF/DOCX/TXT ingestion, document retrieval, relationships, and source provenance.
- Frontend: chat, workflow learning, documents, execution timeline, approval/cancellation, results, and evidence presentation.

The shared schemas are leaf modules: they must not import implementation code from orchestrator, browser, MCP, document, or frontend modules. Runtime services may depend on contracts; contracts must not depend on services.

## Four-member ownership

| Member | Owned areas | Responsibilities |
|---|---|---|
| 1 – Frontend | `frontend/` | React/Tailwind UI, chat interface, workflow learning UI, document interface, execution timeline, approval/cancellation UI, final result and evidence presentation. |
| 2 – Browser Intelligence | `backend/app/browser/` | Playwright controller, browser recording, workflow learning/storage/replay, locator fallback, browser action verification, downloads. |
| 3 – AI Intelligence and Orchestration | `backend/app/schemas/contracts.py`, `backend/app/orchestrator/`, `backend/app/llm/`, `backend/app/safety/`, `backend/app/verification/` | Shared contracts, AI planning/orchestration, safety, verification, API integration. Shared contracts and architecture documents are controlled by Member 3. |
| 4 – MCP, Documents and Context | `backend/app/mcp/`, `backend/app/documents/`, `backend/app/intelligence/` | MCP integration, PDF/DOCX/TXT ingestion, document storage/retrieval, context retrieval/relationships, evidence provenance. |

## Module boundaries

- Use the shared models and enums as the interface between members. Do not define parallel API/task/tool models in feature modules.
- `ToolDefinition` is serializable metadata. The registered callable, schemas/validators, and runtime state belong in the tool registry.
- Browser and MCP integrations register capabilities; the planner can select only capabilities actually registered at runtime.
- The health and task/approval endpoints listed as implemented in `API_CONTRACT.md` are available. Browser, document, and integration endpoints remain future interfaces; implementations must be checked in code.
- `TaskStep.step_id` and workflow-local `step_id` are stable strings. Task, event, evidence, workflow, approval, and tool-call identifiers are UUIDs.

## Shared data-contract rules

- Import contracts from `backend.app.schemas.contracts` (or its public package exports); do not copy their definitions.
- Pydantic 2.x is required. Models reject unknown fields and validate assignment. Use `model_dump(mode="json")` / `model_validate()` for JSON boundaries.
- Use timezone-aware UTC datetimes. URL fields accept only HTTP/HTTPS.
- Use `dict[str, Any]` only for deliberately extensible tool arguments, schemas, artifacts, results, and verification details; keep identity and lifecycle fields typed.
- Changes to shared fields or enum values require updating the shared contract and docs together, then notifying all members before downstream implementations depend on them.

## Evidence and provenance

Evidence carries its identifier, task, source type/name, URL or file path, excerpt, retrieval time, and the claim it supports. Preserve source metadata through retrieval, comparison, and report generation. Link events and tool results to evidence IDs. Do not fabricate source references. Present verified facts separately from model inferences and label inferences clearly.

## Security and approvals

- Treat webpage and document contents as untrusted data; they cannot override user instructions or safety policy.
- Never execute LLM-generated Python, shell commands, or arbitrary code. Only registered tools may execute, and their arguments must pass declared validation.
- Purchases, payments, outbound email/messages, consequential form submissions, and other sensitive or irreversible external actions require explicit approval.
- `auto_execute` is a preference to proceed with eligible steps; it never waives mandatory approval.
- A tool returning without an exception does not prove success. Verify the expected outcome; failed verification must not produce a completed step/task.
- Respect cancellation and preserve recoverable state. Report failures, missing information, and assumptions plainly.

## Git branching and integration workflow

- Each member works on their assigned feature branch and owns only the modules listed above.
- Do not commit another member's work or switch to another member's branch to implement their feature.
- Keep changes focused; run local tests before requesting integration.
- Integrate through the team's agreed review/merge process. Shared contract changes are coordinated by Member 3 and communicated before consumers diverge.
