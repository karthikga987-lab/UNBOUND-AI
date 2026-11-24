# UNBOUND AI End-to-End Integration Test Plan

## Scope and implementation status

This plan follows [`DEMO_SCENARIO.md`](DEMO_SCENARIO.md). The current branch implements intent/planning, task APIs, approvals, verification, recovery, persistent conversations and controlled personal memory. Document PDF/DOCX/TXT ingestion, search, context retrieval/relationships, document API routes, and a standalone read-only MCP server are present. All six browser tools are registered, but the runtime security gate disables browser execution. The loopback proxy and browser request-scheme gate exist; real Chromium egress containment and direct-network bypass resistance are unverified. Browser research is not operational, and Live View is unavailable. Spreadsheet extraction, cross-source comparison, report generation, external MCP client connections, and some frontend panels remain pending or simulated.

The test matrix below is an executable acceptance plan for integration day. Rows needing absent modules are **blocked pending the owning member's real implementation**. Do not substitute fake tool behavior in the flagship rehearsal. Unit and API tests may use fakes.

## Responsibility matrix

| Member | Owns / supplies | Integration responsibility |
|---|---|---|
| 1 — Frontend | `frontend/` React/Vite UI | Call implemented task/health/approval endpoints; display TaskResponse, ExecutionEvent, evidence and task status accurately; mark simulated panels and unavailable browser transport clearly. |
| 2 — Browser | `backend/app/browser/` | Six registered tools (`browser.navigate`, `browser.click`, `browser.fill`, `browser.record`, `browser.replay`, `browser.download`) use a connection-pinned local HTTP/HTTPS proxy and scheme gate; runtime execution remains disabled pending real Chromium egress and bypass verification. |
| 3 — Orchestration | API, planner, runtime, executor, safety and contracts | Maintain API/runtime integration; ensure only registered tools plan/execute; approval, verifier, cancellation and event/evidence semantics; coordinate integration and run this plan. |
| 4 – MCP/docs/context | `backend/app/mcp/`, `documents/`, `intelligence/` | Maintain document ingestion/search/context implementations and provenance; provide spreadsheet extraction and future external MCP client integration only through reviewed allowlisted adapters and tool-specific verifiers. |

## Test matrix

Contract shorthand: task create input is `TaskCreateRequest(user_request, auto_execute=false)`. Responses are `TaskResponse`; plans are `TaskStep` with string `step_id`, registered `tool_name`, dict `input`, expected outcome, status and approval flag. Tool calls use `ToolCall(task_id, step_id, tool_name, arguments)` and callable signature `execute(validated_arguments, tool_call) -> ToolResult`. Events are `ExecutionEvent`; evidence is `EvidenceItem` with task/source provenance. Runtime APIs are `TaskRuntime.register_tool(definition, input_model, execute, verifier=None)` and `TaskRuntime.configure_verifier(tool_name, verifier)`. Verifier signature is `(tool_name, expected_outcome, tool_result) -> bool`.

| ID / stage | Owner | Preconditions and input | Expected tool calls | Expected task status and events | Evidence / verification | Failure conditions |
|---|---|---|---|---|---|---|
| IT-01 Basic task submission | 1 + 3 | API/runtime available; configured test provider; request “Find the latest synthetic quarterly report.” | None at create when `auto_execute=false` | CREATED (or NEEDS_ATTENTION if plan needs clarification); `task.created`; valid plan returned | No evidence yet; check UUID4, shared contract serialization and step IDs | LLM missing gives safe 503; malformed request 422; no invented tools/inputs |
| IT-02 Multi-step plan | 3 | Register real tool metadata for each planned capability; request synthetic report retrieval, download and comparison | Planner names only registered tools in required order | CREATED; plan steps PENDING | Inputs validate against registered Pydantic models | Unregistered capability, duplicate step IDs, or invalid inputs accepted |
| IT-03 Missing information | 3 + 1 | Request omits an essential parameter (e.g. which synthetic organization); fake provider only in component test | No dependent action | NEEDS_ATTENTION; planning/missing-information state visible | Missing value stated; no evidence expected | Planner invents parameter or executor runs dependent action |
| IT-04 Browser navigation | 2 + 3 | **Blocked:** six tools are registered, but the runtime deny gate is closed; real Chromium egress containment and bypass resistance are unverified | No browser action may reach Playwright | No navigation success may be reported | Live navigation and connection-time network enforcement have not been verified | Any browser action reaches Playwright while the gate is closed or without reviewed egress enforcement |
| IT-05 Workflow record and replay | 2 + 3 | **Blocked:** browser execution is disabled; browser workflow/Live View is not operational | No `browser.record` or `browser.replay` dispatch | No recording or replay completion may be reported | Browser evidence unavailable until gated runtime is safely enabled | Replay dispatch, approval bypass, untrusted page instruction, or invented parameters |
| IT-06 Report download | 2 + 3 | **Blocked:** browser execution is disabled; registration of `browser.download` does not make it operational | No `browser.download` dispatch | No download completion may be reported | No live browser download evidence available | A browser download is invoked or reported complete while the gate is closed |
| IT-07 Document upload | 4 + 1 | Synthetic PDF/DOCX/TXT fixtures; document API available | `POST /api/documents/upload`; `document.upload` only when a configured import root is enabled | API reports ingestion success or safe failure; task import requires approval | DOCUMENT evidence includes source name and extraction provenance | Unsupported/corrupt file accepted; path escape; partial ingestion hidden |
| IT-08 PDF extraction | 4 | Synthetic PDF fixture with known rows/values and page references | `document.upload` then retrieval tool(s) | Extraction completion is not conflated with task completion | DOCUMENT evidence includes page/excerpt and supports_claim | Wrong page/value, missing provenance, fabricated extraction |
| IT-09 Spreadsheet extraction | 4 | Synthetic XLSX/CSV fixture with sheet, row and values known | `document.upload` then retrieval tool(s) | Retrieval completes only after expected sheet/row values verified | DOCUMENT evidence identifies filename, sheet/row in excerpt or source reference | Wrong sheet/row or spreadsheet formula/value ambiguity not disclosed |
| IT-10 Context retrieval | 4 + 3 | Documents indexed; context tool and tool-specific verifier registered | `context.retrieve` | VERIFIED step only if returned context supports expected outcome | CONTEXT/DOCUMENT evidence preserves underlying source links | Context answer lacks citations or verifier trusts success flag only |
| IT-11 Cross-document comparison | 3 + 4 | Website report and both synthetic documents retrieved; known comparison fixture | Relevant browser/document/context tools; comparison/report tool only if registered | Task completion after comparison claims independently checked | Evidence references all compared values and conflicting sources | Unsupported resolution, missing conflict, or model inference presented as verified fact |
| IT-12 Final report generation | 3 + 4 + 1 | Evidence and verified comparison available; real `report.generate` registered if used | `report.generate` | COMPLETED only on independent artifact/content verification | REPORT evidence/artifact cites source evidence IDs | Report includes unsupported fact, fabricated citation, or missing artifact marked success |
| IT-13 Evidence provenance | 3 + 4 + 2 | Any tool returns evidence for the current task | Tool call followed by verification | Evidence retained on success and failed verification | Every EvidenceItem task_id matches; event evidence_ids link; source type/name/path or URL is traceable | Cross-task evidence accepted; evidence retention implies success |
| IT-14 Approval and rejection | 3 + 1 + 2 | Sensitive synthetic action in a registered tool; call execute then resolve approval | Sensitive callable not invoked before approval; after APPROVED, explicit execute invokes once; REJECTED never invokes | AWAITING_APPROVAL; approval event; approved step executes only after separate execute request; rejected step SKIPPED/NEEDS_ATTENTION | ApprovalRequest binds exact task/step/tool/arguments; audit event retained | `auto_execute` bypass, approval reuse, rejection executes, approval call auto-runs task |
| IT-15 Failed verification | 3 + tool owner | Tool returns success-shaped result but verifier returns false or raises | Exactly one tool call | FAILED; `verification.failed`; never COMPLETED | Valid tool evidence retained and linked, explicitly not proof of success | Result JSON flag or ToolResult.success substitutes for verifier |
| IT-16 Cancellation | 1 + 3 + tool owner | Multi-step task; cancel before next step; separate test cancels before execution claim | No later action invoked; already-started external call may finish | CANCELLED event/status; completed earlier work retained | Evidence retained for completed work | Claim cancellation reverses external action or later step runs |
| IT-17 Recovery | 3 + tool owner | Failure/interruption after an earlier verified step | No automatic retry of ambiguous failed/running external action; human reconciliation required | Task remains FAILED or moves to NEEDS_ATTENTION | Completed step/evidence preserved; uncertain action is clearly identified | Failed side effect is replayed or represented as verified |
| IT-18 Frontend status updates | 1 + 3 | UI connected to API; delayed fake/local adapter only in component test | Poll task retrieval and events; approve/reject/cancel as explicit requests | UI tracks CREATED, EXECUTING, VERIFYING, AWAITING_APPROVAL, NEEDS_ATTENTION, FAILED, COMPLETED, CANCELLED | Render event ordering and evidence refs from contracts | UI invents status, hides failure, or treats approval as execution |

## API and runtime integration contract

| Boundary | Actual interface / test assertion |
|---|---|
| React → FastAPI | Implemented: health, task/approval, conversation/memory, and document endpoints. Document API supports `POST /api/documents/upload`, `GET /api/documents`, and `POST /api/documents/search`; see `API_CONTRACT.md` for request/response fields and limits. Browser tools are registered server-side but gated; browser workflow routes and Live View transport are not present. |
| FastAPI → runtime | Every route reads the one `request.app.state.runtime`; API tests use `create_app(runtime)` for injection. |
| Runtime → registry | `runtime.register_tool(ToolDefinition, InputModel, execute, verifier=None)`. Registry validates agreed tool name, callable, Pydantic model and exact generated JSON schema. `runtime.configure_verifier(tool_name, verifier)` updates the executor; unknown names fail. |
| Registry → adapter | `execute(validated_arguments, tool_call) -> ToolResult` (JSON-compatible dict may be normalized). ToolCall supplies UUID task ID, string step ID, tool name and arguments. |
| Executor → verifier | Per-tool verifier takes precedence over constructor-level shared fallback: `(tool_name, expected_outcome, ToolResult) -> bool`. Missing verifier blocks invocation; false/exception fails the step. Configuration updates wait for active executor runs. Register adapters during startup; concurrent registry mutation while planning/execution is unsupported. |
| Tool result → evidence | Return `ToolResult(success, data, error, evidence=[EvidenceItem(...)])`; every evidence task_id must match ToolCall.task_id. Preserve source type/name/URL or file_path/excerpt/retrieved_at/supports_claim. |
| Events → UI timeline | `GET /api/tasks/{task_id}/events` returns ordered `ExecutionEvent` values with task_id, optional step_id, event_type, status, timestamp and evidence_ids. Unknown task returns 404. |

## Synthetic test fixture set

All values below are invented for integration testing and must be labeled synthetic in UI/test outputs. They are not claims about any real organization.

- **User request:** “Using the saved Synthetic Metrics Portal workflow, retrieve the latest synthetic Q2 2025 energy report, compare its North Campus usage with the two uploaded synthetic files, and produce a cited summary. Do not submit or send anything.”
- **Browser workflow:** `synthetic_metrics_latest_report`, start URL `https://demo.invalid/synthetic/metrics`, one read-only navigation/search step and one verified report-open step. Test adapter must point only to a controlled local fixture or approved test site, never a production system.
- **Download fixture:** `synthetic_q2_2025_energy.csv`, row `North Campus, 1200 kWh`, report version `2025-07-15` (synthetic fixture timestamp).
- **Document A:** `synthetic_facilities_note.pdf`, excerpt “North Campus Q2 usage: 1,180 kWh” (synthetic).
- **Document B:** `synthetic_energy_ledger.xlsx`, `Q2` sheet, North Campus row `1,200 kWh` (synthetic).
- **Expected comparison:** portal and ledger agree at 1,200 kWh; PDF is 20 kWh lower. Report this as a source discrepancy; do not choose which source is authoritative without an explicit rule/evidence.
- **Expected evidence refs:** distinct EvidenceItem IDs for portal report (BROWSER), PDF (DOCUMENT, page reference in excerpt/source), spreadsheet (DOCUMENT, sheet/row in excerpt/source); comparison/report events link their IDs. Synthetic identifiers must be visibly marked `SYNTHETIC` in fixtures/results.

## Acceptance gate

Do not call the complete flagship demo accepted until all critical criteria pass with real registered modules: frontend submission; valid registered-tool plan; real tool execution; mandatory approval and rejection behavior; independent verification; no success after failed verification; traceable evidence; accurate frontend task/event states; evidence-grounded final report; and no fabricated sources/results. Unit fakes prove orchestration behavior only and do not satisfy real integration acceptance.

Critical blockers today: browser execution is intentionally disabled until real Chromium egress containment and direct-network bypass resistance are verified; spreadsheet ingestion; cross-document comparison/report generation; and end-to-end verification with registered live adapters. The React frontend exists, but some panels remain simulated. PDF/DOCX/TXT ingestion and document/context search have automated fixture coverage; use synthetic fixtures for the demo unless a separate live source is explicitly approved.

## Security and persistence end-to-end sequence

Run this sequence against a temporary SQLite path and a fake provider/tool for automated integration tests. It validates API, conversation, memory, and orchestration boundaries; it does not represent a real browser, MCP server, or document integration. Repeat the conversation/memory restart checks with a second application runtime using the same SQLite path. Task state is intentionally process-local and should be lost on restart without replay.

1. Create conversation A, send a text message, retrieve history, then send a second voice-compatible text message through the same session endpoint. Verify one ordered user/assistant pair per request ID and idempotent retry.
2. Explicitly extract or propose a synthetic memory candidate from a cited user message. Verify it is `pending_approval` and absent from generated assistant context.
3. Approve that memory; create conversation B and ask a relevant follow-up. Verify only approved, confirmed, relevant memory is included and A's transcript is not injected as conversation history.
4. Disable retrieval and repeat the question; verify the memory is absent. Re-enable retrieval and verify it becomes eligible again.
5. Classify an informational request through `/api/assistant/requests`; verify `task=null`, no tool call, and no task state. Also send malicious quoted/history/memory text that asks to ignore policy or perform an action; verify it remains data and does not authorize execution.
6. Submit an action-planning request with a registered test tool. Review the returned plan and verify task status is created, `auto_execute=false`, no callable was invoked, and dependencies/inputs are valid.
7. For a separate explicit execution request, require a separate execute API call. Verify sensitive actions pause for a task-bound approval, rejection never calls the tool, and approval resolution alone does not call the tool.
8. Test changed planned arguments after approval; resolution or execution must reject the stale approval. Test missing verifier, verifier false/exception, tool exception, unmet dependency, and cancellation; no unverified step may be completed. Use only fake callables and assert invocation counts.
9. Verify events retain source evidence IDs and provenance even on verification failure, without presenting retained evidence as successful proof.
10. Delete the approved memory and verify it is absent after a new runtime uses the same database. Re-run candidate extraction over the old source message and verify suppression prevents silent recreation. Verify session deletion cascades messages.
11. Restart the app/runtime using the same SQLite file; verify conversations, memory deletion, and retrieval setting persist. Verify task/event/approval state does not persist and no tools are replayed.
12. Verify middleware blocks a non-loopback peer, invalid Host, untrusted mutating Origin, and body larger than 1 MiB, while the documented loopback voice prototype origin still works.

Only actual registered integrations may close the flagship end-to-end acceptance gate. Fake tests establish control flow and failure behavior; they do not validate Playwright, MCP, extraction quality, external-side-effect reconciliation, or production security.
# Local companion extension checks

- Reminders: verify pending proposal, explicit confirmation, timezone-aware `today`/`tomorrow`, persistence after a new runtime, edit/complete/delete, and no task creation. Notifications while the program is closed are out of scope.
- Wake word: fake-detector tests verify exact phrase/confidence, permission-denied handling, mute/disable, and one-shot handoff to push-to-talk. Real offline activation is blocked until a local model and detector adapter are supplied and measured on target hardware.
- Provider recovery: synthetic tests cover transient/persistent HTTP 503, malformed output without retry, idempotent persistence, and a successful later request. Live Qwen sampling is a manual validation and does not establish reliability under load.
