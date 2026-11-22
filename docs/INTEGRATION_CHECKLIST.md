# UNBOUND AI Integration-Day Checklist

Run the phases in order. Mark a step **blocked** rather than simulating a missing integration. Use only synthetic fixtures unless the team has separately approved real data and targets.

## 1. Pre-integration checks

- [ ] Confirm all members are integrating reviewed changes on the agreed branches; preserve local work.
- [ ] Confirm `python -B -m pytest -p no:cacheprovider backend/tests -q` passes.
- [ ] Start API from repository root in PowerShell:

  ```powershell
  cd backend
  $env:PYTHONPATH = (Resolve-Path ..).Path
  python -m uvicorn app.main:app --reload
  ```

- [ ] Open `http://127.0.0.1:8000/docs`; check `GET /api/health` reports tools honestly.
- [ ] Set `LLM_PROVIDER`, `LLM_MODEL`, and provider-specific settings (`LLM_BASE_URL`, `OPENAI_API_KEY` or `LLM_API_KEY`, `LLM_TIMEOUT_SECONDS`) using local environment/secrets; never paste credentials into fixtures/logs.
- [ ] Confirm each integration uses the agreed synthetic site and synthetic documents; verify no production write targets.
- [ ] Confirm in-memory task state limitation is understood: restarting API loses tasks/events/approvals.

## 2. Member-by-member integration

- [ ] **Member 2:** Register real browser adapters through shared `TaskRuntime.register_tool`; use strict Pydantic inputs, actual callable signature `(validated_arguments, tool_call) -> ToolResult`, browser evidence and per-tool verifiers. Include locator failure, download corruption and replay sensitive-action cases.
- [ ] **Member 4:** Register actual document upload/search/context tools the same way; preserve PDF page and spreadsheet sheet/row provenance. Verifiers must check retrieved data and evidence, not only success flags.
- [ ] **Member 3:** Compose all registrations and verifier strategies before serving requests; confirm names/schema compatibility, planner sees only registered tools, approval policy and resume path.
- [ ] **Member 1:** Connect chat/status/event/approval/cancel/result/evidence UI to documented routes and contracts. Browser workflow teaching and upload screens remain blocked until member-owned routes/interfaces exist.
- [ ] Each owner reports tool names, input schemas, approval metadata, verifier behavior, fixture dependencies and recovery limitations.

## 3. API contract validation

- [ ] `GET /api/health`: no secrets; accurately reports LLM configuration and registered-tool count.
- [ ] `POST /api/tasks`: valid `TaskCreateRequest`; response parses as shared `TaskResponse`; `auto_execute` defaults false.
- [ ] Retrieve, execute, cancel and events routes retain UUID/status/event/evidence fields.
- [ ] Approval route accepts only `APPROVED` or `REJECTED`; resolution does not execute until explicit execute request.
- [ ] Invalid bodies/UUIDs return 422; unknown tasks/approvals return appropriate 404; responses contain no stack traces or credentials.
- [ ] Confirm browser/document upload routes are not mistakenly presented as live if still absent.

## 4. Component tests

- [ ] Run contract, orchestration, API and each member's adapter tests independently.
- [ ] Check duplicate/unregistered tool rejection and exact generated-schema match.
- [ ] Check missing verifier blocks before callable invocation; tool-specific verifier takes precedence; false/exception fails task.
- [ ] Check sensitive action approval, rejection, one-use scope, and no `auto_execute` bypass.
- [ ] Check evidence task ownership, provenance preservation and event evidence-ID links, including failed verification.
- [ ] Check cancellation before call claim, in-flight cancellation caveat, concurrent same-task execution, and recovery without repeating verified steps.
- [ ] Check browser workflow replay, download integrity, PDF page extraction and spreadsheet row/sheet extraction against synthetic expected fixtures.

## 5. Full end-to-end test

- [ ] Submit the synthetic request from `INTEGRATION_TEST_PLAN.md` in the UI.
- [ ] Verify planned steps name only registered tools and have validated inputs/expected outcomes.
- [ ] Run saved workflow against synthetic report fixture; independently verify latest report/version and downloaded file.
- [ ] Upload synthetic PDF and spreadsheet; check extraction evidence points to page and sheet/row.
- [ ] Retrieve context and compare sources; check the expected 20 kWh discrepancy is reported as a discrepancy.
- [ ] Generate final report only from retrieved evidence; inspect citations and verified/inference distinction.
- [ ] Compare UI timeline with API task and event responses at each stage.

## 6. Failure and safety tests

- [ ] Reject approval and prove callable count remains zero; approve separately and explicitly execute once.
- [ ] Force verifier false and exception; ensure no completed status and retain source evidence as unverified.
- [ ] Cancel before action claim and confirm tool is not invoked; separately cancel during a blocking tool and confirm no later step runs. Do not assert rollback of already-started external action.
- [ ] Simulate expired browser session, missing locator, bad download, corrupt PDF, malformed spreadsheet and unavailable context service; verify actionable failure/attention state and no fabricated output.
- [ ] Restart API and observe expected in-memory task loss; communicate this clearly.

## 7. Final demo rehearsal

- [ ] Re-run required backend suite and each integration owner's targeted tests.
- [ ] Reset synthetic browser/document fixtures and confirm stable latest-report ordering.
- [ ] Verify API health, tool registration count, provider readiness, UI connection, and clean presentation data.
- [ ] Rehearse approval/rejection and at least one failure/recovery branch.
- [ ] Confirm no real outbound message, purchase, payment, or consequential form submission can occur in the demo.
- [ ] Assign a driver for UI, a safety observer for approvals/status, and a recorder for blockers; stop the demo if evidence or verification is missing.
- [ ] Record accepted/pending criteria and known limitations before presenting the demo as complete.
