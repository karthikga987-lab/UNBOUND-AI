# UNBOUND AI Project Contract

## Product vision

UNBOUND AI is a general-purpose digital agent that learns reusable browser workflows, connects information across websites and documents, understands natural-language requests, selects registered tools, executes multi-step tasks, verifies outcomes, and returns evidence-backed results. The product must remain domain-neutral.

## System architecture

The backend is organized around shared Pydantic contracts (`backend/app/schemas/contracts.py`) and implementation modules with one-way dependencies:

- API layer (`backend/app/main.py` and `backend/app/api/`): validates requests and returns contract/API models; task state is currently in memory.
- Conversation and controlled personal memory (`backend/app/conversation/`, `backend/app/memory/`, `backend/app/assistant/`): local SQLite transcript persistence is separate from user-approved personal memories; pending proposals are not retrieved as facts. Central assistant instructions and bounded context assembly keep system rules separate from supplied history, memories, and runtime tool metadata.
- Orchestrator and LLM: interpret requests, plan only with registered tools, manage task state, approvals, execution, recovery, and synthesis.
- Browser intelligence: browser control, recording, learned workflow storage/replay, locator fallback, verification, and downloads.
- MCP/documents/context: PDF/DOCX/TXT ingestion, isolated document storage, search, context retrieval/relationships, and source provenance. The integrated MCP server is a standalone, explicitly launched read-only server; this checkout does not automatically connect to external MCP servers.
- Frontend: chat, workflow learning, documents, execution timeline, approval/cancellation, results, and evidence presentation.

The shared schemas are leaf modules: they must not import implementation code from orchestrator, browser, MCP, document, or frontend modules. Runtime services may depend on contracts; contracts must not depend on services.

## Four-member ownership

| Member | Owned areas | Responsibilities |
|---|---|---|
| 1 – Frontend | `frontend/` | React/Tailwind UI, chat interface, workflow learning UI, document interface, execution timeline, approval/cancellation UI, final result and evidence presentation. |
| 2 – Browser Intelligence | `backend/app/browser/` | Playwright controller, browser recording, workflow learning/storage/replay, locator fallback, browser action verification, downloads. |
| 3 – AI Intelligence and Orchestration | `backend/app/schemas/contracts.py`, `backend/app/assistant/`, `backend/app/orchestrator/`, `backend/app/llm/`, `backend/app/safety/`, `backend/app/verification/`, `backend/app/memory/` | Shared contracts, assistant intent routing, AI planning/orchestration, controlled user-approved memory, safety, verification, API integration. Shared contracts and architecture documents are controlled by Member 3. |
| 4 – MCP, Documents and Context | `backend/app/mcp/`, `backend/app/documents/`, `backend/app/intelligence/` | MCP integration, PDF/DOCX/TXT ingestion, document storage/retrieval, context retrieval/relationships, evidence provenance. |

## Module boundaries

- Use the shared models and enums as the interface between members. Do not define parallel API/task/tool models in feature modules.
- `ToolDefinition` is serializable metadata. The registered callable, schemas/validators, and runtime state belong in the tool registry.
- Tools register through the shared `TaskRuntime` and `ToolRegistry`; the planner can select only capabilities actually registered at runtime. Default runtime construction registers the read-only document search/context tools. Local filesystem document import is registered only when `UNBOUND_DOCUMENT_IMPORT_DIR` is explicitly configured and remains approval-gated.
- The health, task/approval, and document endpoints listed as implemented in `API_CONTRACT.md` are available. Browser workflow endpoints and external MCP client connections are not implemented in this checkout.
- `TaskStep.step_id` and workflow-local `step_id` are stable strings. Task, event, evidence, workflow, approval, and tool-call identifiers are UUIDs.
- Task plan dependencies use `TaskStep.depends_on`, listing only prerequisite step IDs that occur earlier in the ordered plan. The executor runs sequentially and refuses to execute a dependent step when any prerequisite is not verified complete.

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
- Personal memories require an explicit save request or user approval; model-extracted candidates remain unconfirmed until then. Users can inspect, correct, disable retrieval, and delete memories. Conversation history alone never authorizes tool actions.
- Conversation is response-only. Information, draft, and planning requests remain conversational; external actions must be explicitly submitted through the task API and then follow the existing approval and verification flow. The conversation endpoint never dispatches a tool or task from transcript or memory context.
- The separate assistant request-routing endpoint validates intent before producing a plan. It may use session history to resolve references, but history and personal memory never grant current-turn execution authorization. Intent classification can create a plan only; task execution remains a separate explicit API operation.

## Local prototype security posture

- The unauthenticated API is restricted to loopback peers, recognized local Host values, and the voice prototype/API origins. Mutating browser requests with an Origin must match the configured local origins. Request bodies are capped at 1 MiB. These controls reduce network and browser-origin exposure; they do not provide user authentication or protect against another local process/user that can connect to loopback.
- Do not bind this prototype to a public/network interface or expose it through a tunnel/reverse proxy. Before multi-user or remote use, add authentication, authorization, per-user data isolation, CSRF protection appropriate to the auth scheme, and deployment-level abuse controls.
- SQLite conversation and memory data are local but not encrypted at rest. Keep the database in a user-private directory and use OS account protections. Provider requests include conversation context and approved relevant memory; Ollama is local when selected, while cloud providers may transmit content externally.
- Website, document, MCP, memory, and history text is untrusted data. System instructions reduce prompt-injection risk but cannot guarantee model compliance. Only the registered-tool boundary may execute actions, with validated inputs, approval, and independent verification.
- Tool/executor errors and unexpected API failures return content-minimized diagnostics. Do not log prompts, memory text, raw tool outputs, secrets, or exception tracebacks in normal operation.
- Conversation and memory persistence use SQLite. Task, event, approval, and execution state remains in memory and is lost on restart. No automatic task replay occurs after restart. Explicit in-process recovery does not retry failed/interrupted steps whose external side effect may be uncertain; create a reviewed replacement task after inspecting the external system.
- This checkout is a local MVP, not production-ready. Rate limiting, authentication, multi-user authorization, encryption at rest, durable task journaling, and operational security review remain prerequisites for broader deployment.

## Git branching and integration workflow

- Each member works on their assigned feature branch and owns only the modules listed above.
- Do not commit another member's work or switch to another member's branch to implement their feature.
- Keep changes focused; run local tests before requesting integration.
- Integrate through the team's agreed review/merge process. Shared contract changes are coordinated by Member 3 and communicated before consumers diverge.
## Reminder and wake-word boundaries

Local reminders are a separate persisted domain from personal memory and task state. Reminder proposals require user confirmation before activation and never authorize or invoke tools. An optional standalone Windows user-session agent can submit native toasts from the shared SQLite database while FastAPI and the browser are closed; it requires explicit per-user installation and cannot notify while Windows is shut down, asleep, or the user is logged out. Submission to Windows is not proof the toast was displayed. Wake-word detection is not implemented without a verified local detector and model; browser Web Speech recognition must not be used for continuous wake detection.
