# UNBOUND AI Integration Risks and Blockers

Status reflects the inspected `feature/orchestration-engine` checkout. “Pending” means no implementation was found here; it is not evidence that another member has no work elsewhere.

## Critical demo blockers

| Risk | Impact | Owner | Mitigation / exit check |
|---|---|---|---|
| Browser module and workflow storage/replay/download are absent | Cannot retrieve the website report or run the flagship workflow | Member 2 | Supply real adapters, controlled synthetic target, observable verification and download integrity tests; register only real capabilities. |
| Document/MCP/context modules are absent | Cannot upload/extract PDF or spreadsheet, retrieve relationships, or preserve real provenance | Member 4 | Supply real ingestion/retrieval adapters; test PDF page and workbook sheet/row references against synthetic fixtures. |
| Frontend directory is absent | User cannot perform the full demo through a UI or inspect live timeline/evidence | Member 1 | Supply frontend and connect to implemented task API; add UI contract tests for statuses and approval flow. |
| No real tool-specific verifiers are registered by default | Executor safely refuses tool invocation without verification | Members 2, 4, coordinated by 3 | Each adapter provides observable result data/evidence and an appropriate verifier via `TaskRuntime.register_tool(..., verifier=...)`. Do not use success-only verifiers. |
| Browser/document workflow and upload HTTP APIs are absent | Frontend cannot yet teach workflows or upload documents through described routes | Members 1, 2, 4; coordinate with 3 | Agree member-owned routes/interfaces and models before UI integration; current implemented API is health/task/approval only. |

## High and medium risks

| Risk | Impact | Current mitigation / required test |
|---|---|---|
| Task/event/approval state is process memory | Restart loses tasks; multiple worker processes do not share state | Keep one process for MVP rehearsal; disclose limitation. Do not claim durable recovery. |
| Verifier or registry changes during execution | Could create a safety/configuration race | Verifier update waits until active executor runs finish and applies to later runs. Register tools during startup; concurrent registry mutation during planning/execution is unsupported. |
| Browser replay hides stored workflow actions behind an ID | Action sensitivity cannot be fully classified from caller arguments | Replay requires approval under current policy; Member 2 must preserve per-action verification and avoid hidden consequential actions. |
| External cancellation semantics | In-flight remote action may already have taken effect | Cancellation prevents later steps; it cannot guarantee rollback of an already-started action. Test both before-claim and in-flight cases and communicate outcome honestly. |
| Evidence may be mistaken for proof of success | UI/report could overstate unverified claims | Evidence is retained with provenance on verification failure; display verification status separately and never render retained evidence as success by itself. |
| Synthetic fixture can be confused with research | Demo could present invented values as real findings | Mark all fixture docs, site data, IDs, citations and output SYNTHETIC; never mix fixture facts with real-world claims. |
| Shared test environment/provider settings drift | Failures or accidental external requests during integration | Use fake providers in tests, controlled local browser fixtures, explicit provider configuration and no live write targets. |

## Current implemented versus pending

- **Implemented in this checkout:** shared Pydantic 2 contracts; intent and planning; FastAPI health/task/execute/cancel/events/approval routes; shared injectable TaskRuntime; ToolRegistry schema/input validation; task state and events; approval gates; per-tool/shared verifier selection; verifier update synchronization against active runs; evidence ownership/retention; cancellation and recovery tests.
- **Pending in this checkout:** real browser adapters, workflow recording/storage/replay/download, document upload/storage/PDF/spreadsheet extraction, MCP integration, context retrieval/relationships, comparison/report tool implementations, frontend UI and browser/document-specific HTTP routes.
- **Known runtime limit:** in-memory task state does not survive restart or coordinate across multiple worker processes. Use one API worker for the MVP unless runtime state is redesigned.

## Go/no-go

Do not advertise the flagship as end-to-end ready until each critical blocker has a real implementation, is registered under its agreed name, has a nontrivial verifier, passes its component test, and passes the synthetic full-flow rehearsal with source-linked evidence and correct UI state. Unit tests using fake tools validate orchestration only; they do not close integration blockers.
