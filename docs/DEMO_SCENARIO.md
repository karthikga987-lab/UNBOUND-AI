# Flagship Demo Scenario and Submission Guide

> Public browser research remains disabled because real Chromium egress containment is unverified. An opt-in **LOCAL DEMO MODE** is available for a fixed synthetic page served by the local managed proxy; it uses real Chromium, the existing TaskManager/Executor, registered navigation/click tools, independent verification, and local Ollama summarization. The frontend shows task-backed observations and progress, but does not provide screenshot streaming or remote browser controls. Do not present the fixture as public-web access.

> **Submission status (24 Nov 2026):** The local browser fixture flow passed through the real FastAPI assistant and execute APIs using Ollama and installed Chromium: a task was prepared, explicitly executed, two browser steps independently verified, two evidence items returned, and a local-model summary generated. The final real React/Edge interaction was not verified in this pass because computer control stopped when it could not confidently identify the browser URL. Public browser research and OS-level Chromium network containment remain unverified and disabled. Wake-word detection is unavailable without the custom model; workflow/document/integration panels marked by the UI are simulated; task/event/approval state is lost on backend restart. A synthetic due reminder was submitted through the Windows toast API and persisted as `submitted`; Windows did not report whether it was visibly displayed.

## Flow

1. The user teaches UNBOUND AI a reusable website workflow. **Not currently demonstrable:** browser execution is disabled by the runtime security gate.
2. The agent records the interactions and saves a reusable workflow definition.
3. The user uploads a PDF and a spreadsheet containing related information.
4. The user asks the agent to retrieve the latest report from the website.
5. The agent executes the saved workflow, validating parameters and verifying browser outcomes after actions.
6. The agent retrieves relevant information from the uploaded documents.
7. The agent compares website and document information, preserving sources and noting conflicts or missing data.
8. The agent generates a report with evidence references and citations.
9. The UI displays the execution timeline, verification state, evidence provenance, and final report artifact.

## Expected behavior

- The plan names only tools registered at runtime, with validated arguments and expected outcomes.
- Read-only navigation and retrieval may proceed without approval. Any purchase, payment, outbound communication, or consequential submission pauses for explicit approval even if auto-execution was requested.
- A tool call is not considered complete merely because it returned. Browser steps and other claims are verified; failed verification remains failed or needs attention.
- The final response separates verified facts from inferences, cites source evidence, and identifies assumptions or missing information. It does not invent a report, value, source, or missing parameter.
- Cancellation stops subsequent actions and is reflected in task state. Verified completed steps/evidence are retained in the current process. Failed or interrupted actions with uncertain external effects are not automatically retried; they require human reconciliation and a reviewed replacement task.

## Failure and recovery cases

| Failure | Expected behavior |
|---|---|
| Website unavailable or login/session expired | Record the failed step and safe error; do not claim the report was retrieved. Request user action or allow a recoverable retry. |
| Locator missing or page layout changed | Use only approved locator fallback behavior; verify the resulting page/action. If verification fails, stop/replan or request attention. |
| Required workflow parameter missing | Ask for clarification before executing dependent steps. Do not invent a value. |
| Document is unsupported, corrupt, or unreadable | Report ingestion failure and continue only if the task can safely proceed without it; disclose incomplete comparison. |
| Website and document values conflict | Preserve both sources, label the discrepancy, and avoid asserting an unsupported resolution. |
| Sensitive action appears in the workflow | Pause at that step for explicit approval; auto-execute never bypasses the gate. |
| Browser/tool returns success but expected outcome is absent | Mark verification failed; do not mark the step/task completed. Preserve relevant tool evidence and error. |
| User cancels during execution | Stop before the next side effect, retain completed work and evidence, and set the task to cancelled. |
| Report generation fails | Retain gathered evidence and completed retrieval steps; report the missing artifact and allow recovery. |

## Local prototype security limits

The API is unauthenticated and loopback-only; this does not protect against another local process. Conversation and memory SQLite data are not encrypted at rest. Task/event/approval state is process-local and lost on restart; restart does not replay task actions. Prompt-injection instructions are defense-in-depth and do not replace tool validation, approvals, or independent verification. This MVP must not be exposed to a network or presented as production-ready.

## Current demonstrable MVP sequence

Run the local browser demo only with the explicit environment setup below. For document analysis, use a separate synthetic PDF, DOCX, or TXT file. Rehearse with the exact model and local application configuration that will be used for recording; automated component tests do not establish the React/Edge presentation.

### Opt-in local browser demo

Open a PowerShell window in the repository and configure an isolated local-demo backend. This proxy serves only two fixed synthetic pages at `http://unbound-demo.invalid/product` and `/details`; it does not resolve or connect to external destinations. Only the registered navigation tool and the exact fixture “View details” link are enabled. Public browser execution remains disabled.

```powershell
$demoDir = Join-Path $env:TEMP 'unbound-local-demo'
New-Item -ItemType Directory -Force -Path $demoDir | Out-Null
$env:UNBOUND_BROWSER_LOCAL_DEMO = '1'
$env:UNBOUND_BROWSER_LOCAL_DEMO_HEADLESS = '0'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.playwright-browsers'
$env:UNBOUND_CONVERSATION_DB = Join-Path $demoDir 'conversations.sqlite3'
$env:UNBOUND_DOCUMENT_STORAGE = Join-Path $demoDir 'documents'
$env:UNBOUND_BROWSER_WORKFLOW_DIR = Join-Path $demoDir 'workflows'
$env:UNBOUND_BROWSER_REPLAY_STATE_DIR = Join-Path $demoDir 'replay'
$randomKey = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$env:BROWSER_REPLAY_STATE_KEY = "test:$randomKey"
$env:LLM_PROVIDER = 'ollama'
$env:LLM_MODEL = 'qwen3.5:4b'
$env:LLM_BASE_URL = 'http://localhost:11434/v1'
$env:LLM_TIMEOUT_SECONDS = '120'
.\.venv-wake\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

In a second PowerShell window, start React without changing `.env.local`:

```powershell
Set-Location "$env:USERPROFILE\UNBOUND-AI\frontend"
$env:VITE_USE_MOCK = 'false'
$env:VITE_BACKEND_PROXY_TARGET = 'http://127.0.0.1:8001'
npm run dev -- --host 127.0.0.1 --port 5175 --strictPort
```

In Chat, submit: **“Open the Demo Product page at http://unbound-demo.invalid/product, click the View details link, and tell me its version and features.”** The assistant prepares a fixed two-step local plan and performs no browser action until the user selects **Run plan**. This read-only fixture does not require a consequential-action approval. Expect verified observations for version `2.1` and features `document search, task planning, evidence summaries`. Those values come from the synthetic fixture, not a real website. Stop both processes with `Ctrl+C` when finished.

1. Start the backend and React frontend using the commands above for LOCAL DEMO MODE. Confirm the UI identifies the synthetic fixture; note any mock-data banner.
2. Ask a normal informational question in Chat. Confirm the answer appears in the conversation and no task is created.
3. Ask UNBOUND to read and analyze the exact absolute path of the synthetic document. The prepared task must show the selected file and say it has not been opened.
4. Explicitly start the task to create the approval request. Review the path and arguments, then approve. Confirm approval alone leaves the task unexecuted.
5. Select **Run plan** as a separate action. Show the task status and independent verification result.
6. Show the structured document summary/findings, quoted excerpts, evidence links, and analyzed-chunk scope. Any unsupported or omitted requested topic must remain labeled as missing or partial.
7. Close by stating that the verified browser use is limited to the fixed local fixture. The Live View shows task-backed observations/events, not screenshots or direct browser control. Do not demonstrate simulated workflow recording/replay as real browser automation.

Do not use a real personal document for the recording. Avoid purchases, messages, external form submissions, or other consequential actions. If Ollama or the local document workflow fails during rehearsal, report the safe failure and do not narrate an unverified success.

## Short slide outline

## Final presentation outline (9 slides)

| Slide | Title and content | Suggested visual |
|---|---|---|
| 1 | **UNBOUND AI: a local-first personal agent.** Introduce the goal: assistance that can reason about requests while keeping people in control of actions. | Product title beside a simple user-to-agent flow. |
| 2 | **The problem: answers and actions are different.** A conversational answer should not silently become a consequential operation. | Two lanes: conversation vs. task execution. |
| 3 | **The solution and architecture.** React chat → FastAPI → assistant/planner → central ToolRegistry → local stores and configured LLM. | Architecture diagram; label Ollama as local only when configured. |
| 4 | **Understand, plan, then pause.** A task is prepared with validated registered tools and visible parameters. | Prepared synthetic document task with exact file and “not opened yet.” |
| 5 | **Permission is a gate, not a trigger.** Approval is bound to the pending action; it does not run it. A separate Run plan action is required. | Approval state followed by a distinct Run plan button. |
| 6 | **Evidence-backed local document analysis.** Show supported PDF/DOCX/TXT extraction, source excerpts, citations, verification, and analyzed-chunk scope. | Synthetic report result with one citation highlighted. |
| 7 | **Local-first controls and boundaries.** Conversation/memory/reminders use local SQLite; approved memory can be controlled; microphone activation is explicit. | Data-boundary diagram with local DB and user approval icons. |
| 8 | **What is not ready yet.** Public browser use disabled pending OS-level containment; Live View has no screenshot stream; wake-word model unavailable; selected panels simulated; task state not durable across restart; notification delivery unverified; no API authentication; SQLite is unencrypted at rest. | Honest checklist with “available / pending” labels. |
| 9 | **Next milestone: broaden safely.** Independently verify Chromium process containment before public sites, then add screenshot transport and broader browser workflows. | Roadmap with network and UI gates before public browser actions. |

## YouTube demo script (about 4 minutes)

**0:00–0:25 — Introduce the project**

“UNBOUND AI is a local-first personal assistant prototype. Its key design choice is to keep conversation separate from action: it can answer a question without creating a task, and it must prepare and verify a task through controlled steps.”

**0:25–0:55 — Ordinary conversation**

In the React Chat, ask a short informational question. Say: “This uses the conversation path. It should show a reply without creating an execution task.” Do not claim the final UI path is verified until the manual walkthrough is completed.

**0:55–1:35 — Prepare a synthetic document request**

Use a synthetic TXT, PDF, or DOCX file created for the demo. Ask UNBOUND to read and analyze its exact absolute path. Show the prepared task, file name/path, operation, and statement that it has not been opened. Do not use a personal document.

**1:35–2:10 — Approval does not execute**

Start the prepared task to expose its approval request. Review the exact action, approve it, and show that the task remains unexecuted. “Approval authorizes this exact action; it is not the Run plan command.”

**2:10–3:15 — Explicit execution and evidence**

Click **Run plan** separately. Show the final task status, successful independent verification, document-analysis summary, findings, source quotation, evidence link, and analyzed-chunk coverage. Call out any requested section that is missing or only partially covered. If extraction, analysis, or verification fails, stop and show the honest failure state; do not narrate success.

**3:15–4:00 — Explain safety and limits**

“UNBOUND executes only validated registered tools. This browser run was LOCAL DEMO MODE: Chromium accessed a fixed synthetic fixture through a deny-by-default proxy. The result was independently verified before the local model summarized it. Public-site browsing is still disabled because OS-level process containment has not been verified. Live View has no screenshot stream, wake-word detection has no custom model, selected panels are simulations, task state is in memory, and Windows notifications were not tested today. This is a local prototype, not a production-secure system.”

**Before recording:** Manually verify the live UI’s conversation, prepare, approval pause, Run plan, completed status, evidence/citations, verification and scope display. The automated tests and API smoke are not proof of this browser-level presentation.

## Short project description

UNBOUND AI is a local-first personal assistant prototype that combines conversation, approved personal memory, reminders, document extraction, and a controlled task orchestrator. It separates answering from acting: plans use validated registered tools, sensitive actions pause for explicit approval, and task success depends on independent verification. Demonstrated scope includes local conversation, synthetic PDF/DOCX/TXT analysis, and a fixed LOCAL DEMO MODE browser fixture running through real Chromium and the task executor. Public-site browsing, screenshot streaming, wake-word recognition, durable task history, and some frontend panels remain unavailable, unverified, or simulated.

## Safety architecture explanation

Conversation and task APIs are distinct. An assistant classification or plan does not execute a tool. The backend planner may select only tools in the central ToolRegistry, whose Pydantic schemas validate inputs. TaskManager owns approval state, scoped to the proposed action and arguments. The user must approve when required and then separately invoke execution. The executor enforces dependencies, cancellation, registered-tool boundaries, and an independent verifier; missing or failed verification cannot be reported as success. Documents, retrieved memory, and tool output are treated as untrusted data and cannot authorize actions. These controls reduce risk but do not eliminate model error or prompt injection. The local API has no authentication, and SQLite is not encrypted at rest.
