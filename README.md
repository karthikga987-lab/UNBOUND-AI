# UNBOUND AI backend

## Start locally (PowerShell)

From the repository root:

```powershell
py -3.14 -m venv .venv-wake
& .\.venv-wake\Scripts\python.exe -m pip install -r .\backend\requirements.txt
$env:PYTHONPATH = (Resolve-Path .).Path
$env:LLM_PROVIDER = "ollama"
$env:LLM_MODEL = "qwen3.5:4b"
$env:LLM_BASE_URL = "http://localhost:11434/v1"
$env:LLM_TIMEOUT_SECONDS = "120"
& .\.venv-wake\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8001
```

The API is at `http://127.0.0.1:8001/api`; Swagger is at `http://127.0.0.1:8001/docs`. LLM variables are optional to start the application but required for generated task plans and conversation replies.

In a second PowerShell window, start the React frontend:

```powershell
Set-Location .\frontend
npm ci
$env:VITE_USE_MOCK = "false"
$env:VITE_BACKEND_PROXY_TARGET = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

Open `http://127.0.0.1:5173`. The checked-in `.env.local` uses the real backend for chat/tasks and explicitly enables mock data for workflows, documents, and integrations; those panels label their simulated behavior. Clear `VITE_MOCK_AREAS` and restart Vite only after the corresponding real API integration is available.

The backend restricts network peers to loopback, validates local Host/browser Origin values, and caps request bodies to configured limits. This remains an unauthenticated single-user local prototype: any local process that can connect to loopback can access conversations, memories, and task routes. Do not bind/expose it to a network, tunnel, or untrusted proxy. There is no rate limiter or multi-user authorization. SQLite data is not encrypted at rest; provider requests contain conversation context and relevant approved memories, so a cloud LLM provider may transmit them off-device. This project is not production-ready.

Conversation sessions persist in a local SQLite file at `%LOCALAPPDATA%\UNBOUND_AI\conversations.sqlite3` on Windows. Set `UNBOUND_CONVERSATION_DB` before startup to choose another local file. The API currently has no authentication or multi-user isolation; only use it as a trusted local prototype. The SQLite file is not encrypted, and messages go to the configured provider (Ollama is local; a cloud provider may transmit them off-device). Conversation history is not personal-profile memory and does not trigger task execution.

The normal FastAPI entry point also persists task snapshots, approvals, evidence, and ordered events in the separate `%LOCALAPPDATA%\UNBOUND_AI\tasks.sqlite3` database. Override the path with `UNBOUND_TASK_DB`. Task state is loaded after restart and appears in the React Task history tab and `GET /api/tasks`. Interrupted external actions are moved to `needs_attention` and are never automatically retried; trusted browser replay approvals are invalidated on restart. See [task persistence and recovery](docs/TASK_PERSISTENCE.md). The default `TaskRuntime` used in isolated tests remains in-memory unless a SQLite task repository is explicitly provided.

Controlled personal memory uses separate tables in this SQLite file. Candidates extracted from a conversation require approval before retrieval; an explicit request to remember a specific item can save it as confirmed. Memory can be inspected, corrected, deleted, and disabled for response retrieval through `/api/memories` endpoints. See [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) for routes and the single-user security limits. The API has no authentication, and SQLite is not encrypted at rest.

Conversation replies use centralized UNBOUND assistant instructions plus bounded recent history, relevant approved memories (when retrieval is enabled), and the live registered-tool inventory. The React chat first calls the backend intent-classification endpoint: ordinary conversation, information, and drafting turns use persistent conversation sessions; action/clarification turns use the assistant task-preparation endpoint. Ordinary conversation never invokes task tools or creates tasks. Prepared plans never execute from chat; the task workspace requires a separate Run plan action after any approval. Explicit reminder commands use the same local reminder service as `/api/reminders`: UNBOUND asks for missing details, saves a proposal, and waits for confirmation before activation. The browser sends its IANA time zone for relative dates. Reminders are not tasks or personal memories. The independent Windows agent can submit notifications while the browser and backend are closed, but only while Windows is running and the user is logged in. Plan steps expose `depends_on` prerequisites, and the executor will not run dependents when a prerequisite failed, was rejected, or was otherwise not verified complete. The isolated voice prototype offers a separate classify/plan path and consent-gated task execution; recognized speech alone does not execute tasks. Default runtime registers verified read-only document search/context tools. Document upload/list/search APIs and the standalone read-only MCP server are available; local import as a task tool is opt-in and approval-gated. The React chat and task workspace are implemented. The current frontend `.env.local` explicitly simulates workflow recording/replay, document UI data, and integration statuses; it labels these areas as demo-only. Six browser tools are registered. Public-site browsing and general browser actions remain fail-closed because Chromium process-level egress containment is unverified. Opt-in LOCAL DEMO MODE is an exception limited by a deny-by-default proxy to two fixed synthetic pages and the exact “View details” link; it uses real Chromium through the task executor and independent verifiers. Browser Live View does not provide screenshot streaming or remote control. Spreadsheet extraction, external MCP client, and cross-document comparison/report generation remain unavailable. See [the voice prototype guide](experiments/voice-prototype/README.md), [document/MCP integration guide](docs/DOCUMENT_MCP_INTEGRATION.md), and [the demo guide](docs/DEMO_SCENARIO.md).

Browser replay encryption uses `BROWSER_REPLAY_STATE_KEY` in `<key-id>:<base64url-encoded-32-byte-key>` format. Missing or invalid configuration disables encrypted replay state; key material is never logged. Workflow files default to `backend/generated_data/workflows` and replay state defaults to `backend/generated_data/replay_state`, configurable with `UNBOUND_BROWSER_WORKFLOW_DIR` and `UNBOUND_BROWSER_REPLAY_STATE_DIR`. Browser HTTP/HTTPS traffic is configured to use a loopback proxy that rejects non-global destinations, validates every DNS answer, and connects to a validated numeric IP; the Playwright context also blocks non-HTTP(S) schemes. However, Playwright/Chromium egress bypass behavior has not been live-verified in this environment, and no OS-level outbound firewall is in place. Navigation, clicks, fills, recording, replay, and downloads therefore remain blocked pending independent security review. `backend/requirements.txt` declares Playwright and cryptography, but installing dependencies or browser binaries does not remove that runtime gate.

The default runtime also supports explicit single-file local analysis through `POST /api/assistant/requests`, for an exact absolute Windows path to PDF, DOCX, or TXT (20 MiB maximum by default). It prepares an approval-gated task and does not read the file until the user approves the exact path and explicitly executes again. The content is stored in the existing local document store and analyzed using extracted evidence with loopback Ollama only. The ordinary conversation endpoint cannot access files. UNC paths, directories, links/junctions, unsupported formats, and file changes after approval are rejected. This does not provide folder browsing or file modification. SQLite/document text is not encrypted at rest.

### Try controlled memory from PowerShell

With the API running, create a conversation, add a synthetic test message, extract a candidate, inspect it, and approve it explicitly:

```powershell
$base = "http://127.0.0.1:8001/api"
$session = Invoke-RestMethod -Method Post -Uri "$base/conversations"
$exchange = Invoke-RestMethod -Method Post -Uri "$base/conversations/$($session.session_id)/messages" -ContentType "application/json" -Body (@{ message = "Synthetic test: I prefer concise project summaries." } | ConvertTo-Json)
$candidates = Invoke-RestMethod -Method Post -Uri "$base/memories/extract" -ContentType "application/json" -Body (@{ session_id = $session.session_id } | ConvertTo-Json)
$candidates | Format-Table memory_id, category, status, user_confirmed, content
$candidate = $candidates | Select-Object -First 1
if ($null -eq $candidate) { throw "No candidate was extracted; review the conversation or submit a proposal manually." }
```

Review the candidate and approve only the specific item you confirm:

```powershell
$memoryId = $candidate.memory_id
Invoke-RestMethod -Method Post -Uri "$base/memories/$memoryId/approve"
```

Disable or re-enable retrieval without deleting saved memories:

```powershell
Invoke-RestMethod -Method Patch -Uri "$base/memories/settings" -ContentType "application/json" -Body '{"retrieval_enabled":false}'
Invoke-RestMethod -Method Patch -Uri "$base/memories/settings" -ContentType "application/json" -Body '{"retrieval_enabled":true}'
```

For the push-to-talk prototype, see [experiments/voice-prototype/README.md](experiments/voice-prototype/README.md).

The local reminder API, voice prototype, and Windows notification agent share the same SQLite reminder database. Reminders default to `proposed` and require confirmation before they become active. On Windows, the separately installed and running user-session agent polls active reminders and submits due toasts without the backend or browser. An all-day reminder is submitted at 9:00 AM in its stored IANA timezone. Overdue reminders are submitted once, at most one per poll, after the agent resumes; reminders remain stored while the computer is off, but no toast can appear until Windows is running and the user is logged in. Windows does not confirm that a toast was visibly displayed, so the API reports “submitted to Windows,” not “delivered.” On 24 Nov 2026, an isolated synthetic due reminder reached the Windows toast API using the standalone agent while FastAPI was stopped; its `submitted` state persisted. Visible toast display was not independently confirmed. Install the optional dependency with `python -m pip install -r backend/requirements-windows-notifications.txt`, install/update the user task with `scripts/reminder-agent.ps1 install -Python (Resolve-Path '.\.venv-wake\Scripts\python.exe').Path`, and check the toast path with `.\.venv-wake\Scripts\python.exe -m backend.app.reminders.agent --test-notification`. The task runs independently of FastAPI and the browser while the user is logged in. See the [voice prototype guide](experiments/voice-prototype/README.md).

The opt-in “Hey UNBOUND” detector now selects the local openWakeWord runtime and a custom ONNX model; no Picovoice key is required. The user must explicitly enable microphone capture. The exact model is not trained/installed yet, so live recognition is not ready. Setup and dataset/license limitations are documented in the [voice prototype guide](experiments/voice-prototype/README.md) and [model license notes](docs/WAKE_WORD_MODEL_LICENSES.md). Browser speech recognition after activation may still use an online service.

Windows toast delivery depends on Windows notification policy and user-session availability; the agent reports API submission rather than confirmed visual display. See [Microsoft's desktop toast requirements](https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid).

Optional private S3 support includes explicit task-database backup/restore and an opt-in automatic snapshot synchronizer. Background snapshots contain the task/evidence DB plus the controlled document metadata/files/text, use SQLite's online backup API, content-addressed keys, bounded retries, size/SHA-256 verification, and requested SSE-S3. Automatic upload is disabled unless `UNBOUND_S3_SYNC_ENABLED=1`; review the data scope before opting in. The React S3 panel exposes status, manual sync confirmation, task database restore, and verified application snapshot restore to a new recovery directory. A synthetic application snapshot upload was live-tested against the configured bucket/profile, with the object size and SHA-256 metadata verified; automated tests cover recovery validation. The AWS profile could list and perform object operations, but `GetBucketLocation` and `GetBucketVersioning` were denied. S3 remains a local prototype integration: there is no API authentication and Origin checks do not protect against other local processes. See [S3 storage](docs/S3_STORAGE.md).
