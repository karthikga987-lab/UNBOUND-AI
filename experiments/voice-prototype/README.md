# Voice Companion Prototype

This isolated prototype uses browser Web Speech APIs for user-initiated push-to-talk recognition and speech synthesis. Ordinary messages go to the persistent conversation-session API (`/api/conversations`). A message that begins with an explicit read/analyse/summarise/review action and includes an absolute Windows drive path is instead routed to the existing `/api/assistant/requests` preparation flow. Preparation creates no file read; the user must explicitly start the task, approve the exact file action, and explicitly execute again. Browser local storage contains only the current conversation session UUID; task state remains in memory and is not restored after a backend restart. Browser Web Speech recognition may use an online recognition service; this prototype does **not** provide offline speech recognition. Speech synthesis is performed by the browser; its processing location depends on the selected browser voice and is not guaranteed to be local.

## Start the backend

In a PowerShell window from the repository root:

```powershell
$env:LLM_PROVIDER = "ollama"
$env:LLM_MODEL = "qwen3.5:4b"
$env:LLM_BASE_URL = "http://localhost:11434/v1"
python -m uvicorn backend.app.main:app --reload
```

Ollama must already be running with `qwen3.5:4b` available. No model download is performed by this prototype. Verify `http://127.0.0.1:8000/api/health` and `http://127.0.0.1:8000/docs`.

## Start the prototype page

In a second PowerShell window from the repository root:

```powershell
cd experiments/voice-prototype
python -m http.server 8001 --bind 127.0.0.1
```

Open `http://127.0.0.1:8001` in Microsoft Edge. The backend permits cross-origin requests only from `http://127.0.0.1:8001` and `http://localhost:8001` for this prototype.

## Manual Edge voice test

1. Confirm Windows has a working microphone and Ollama responds locally. Open the page at the localhost URL above; localhost is used so Edge can request microphone permission.
2. Keep both toggles enabled. Press **Start listening**, grant microphone access, speak a short phrase, then press **Stop listening**. Check the LISTENING state, partial transcript, and finalized editable text. Try **Cancel recording** and confirm it discards the current recording's recognized text while preserving text that existed before recording.
3. Edit the transcript and press **Send to companion**. Confirm PROCESSING then the response and SPEAKING state. While a long reply is speaking, press **Stop speaking** and confirm the audio stops immediately. Also test pressing Stop during PROCESSING before a response arrives: the reply should still appear as text but must not start speaking automatically. Then press **Speak response** and confirm playback works again. Toggle speech output off and confirm the text response remains visible.
4. Send a follow-up question, reload the page, and verify the same session history is restored from the backend and the follow-up uses stored context. An ordinary follow-up should not create or execute a task.
5. Disable speech recognition and type a message to confirm manual entry remains available. Re-enable it and test denied permission through Edge site settings; the page should show an actionable error.
6. Disable speech synthesis and submit another message; text response should remain available without audio.
7. Use **New conversation** to create a separate empty session. Use **Delete current conversation** to delete the session and all its messages, then confirm the prototype starts a fresh session. Test empty text, retry, unsupported recognition where available, denied microphone permission, missing microphone, silence/timeout, synthesis failure, and a backend/Ollama outage. Expected states are IDLE, LISTENING, PROCESSING, SPEAKING, or ERROR with a useful message.
8. For a local-file handoff, send an explicit request such as `Read C:\Users\Example\Documents\report.txt and summarise it.` through the ordinary **Send to companion** control. Confirm it routes to task preparation, shows the exact path, and does not read the file. Select the consent checkbox and press **Execute task (explicit)**; confirm execution pauses for the exact-file approval. Approve, then separately execute again. Confirm completion appears only after backend verification and includes quoted evidence; a failure must be shown without a success claim. For other task handoffs, **Classify request and prepare plan** remains available. Consequential steps must pause for their own approval; rejection must prevent execution. Unavailable tools must be reported as unavailable, not simulated.

## Voice and task boundaries

- Recognition starts only from a user click and stops at the end of a single recognition session or when canceled. No raw audio is stored. `no-speech` and 30-second listening timeout are reported; the transcript remains editable.
- Final recognition results are deduplicated; interim text is shown separately and is not automatically sent. If the browser lacks `SpeechRecognition` / `webkitSpeechRecognition`, text entry still works.
- Ordinary **Send to companion** messages call the persistent conversation endpoint. Only an explicit supported local-file action at the start of the current message paired with a Windows drive path is deterministically routed to `/api/assistant/requests`; that endpoint only prepares a task. Other recognized text never invokes the task classifier or task execution APIs.
- **Classify request and prepare plan** is a separate explicit API action. Planning returns a task with `auto_execute=false`; starting it requires the separate consent checkbox, execute button, and browser confirmation. Pending consequential approvals have separate approve/reject confirmations, and approval does not start execution.
- The task view reports backend status, per-step status/error/verification data, evidence references, and execution events. It does not treat retained evidence as proof of success. The backend enforces registered tools, mandatory approvals, and verifiers.
- Session history is retrieved from the local backend after page load. Only the current session UUID is stored in `localStorage`; task state is not persisted by this page.
- Microphone access requires a browser-supported secure context, typically `localhost` or HTTPS. Browser speech recognition may send audio to the browser vendor's online recognition service. Speech synthesis may use browser-provided or system voices; local processing is not promised.
- The API has no authentication or multi-user isolation. Keep it bound to a trusted local environment; do not expose it on a network. Conversation SQLite is not encrypted at rest.

## Browserless UI tests

With Node.js installed, run the dependency-free tests from the repository root:

```powershell
node --check experiments/voice-prototype/voice.js
node --check experiments/voice-prototype/wake-word-controller.js
node --check experiments/voice-prototype/tests/voice.test.js
node --check experiments/voice-prototype/tests/wake-word.test.js
node --test --test-reporter=spec experiments/voice-prototype/tests/voice.test.js experiments/voice-prototype/tests/wake-word.test.js
```

These tests use mocked browser APIs and HTTP responses. They do not verify actual Edge microphone permissions, browser recognition services, or a live LLM.

Speech recognition support and behavior depend on Edge/browser services and connectivity; do not describe it as offline. The local SQLite conversation store has no authentication and is intended only for a single-user local prototype. It is conversation history, not automatically extracted personal-profile memory. Task state remains process-local and independent.

Personal memory management currently uses the backend API (`/api/memories`) and has no dedicated controls in this prototype UI. The chat does not automatically extract or save memories; use the proposal/extraction and approval endpoints deliberately. See the API contract before storing personal information.

## Reminders

The prototype provides a local reminder form and conversational reminder commands. New reminders remain proposals until confirmed. Reminder state and notification attempt history share the existing SQLite file at `%LOCALAPPDATA%\UNBOUND_AI\conversations.sqlite3` (override with `UNBOUND_CONVERSATION_DB`). The browser shows notification agent status and each reminder's notification submission state. `submitted` means Windows accepted the toast API call; Windows does not provide a reliable display acknowledgment, and notification settings or Focus Assist may suppress the toast.

### Optional Windows 11 notification agent

The standalone Python user-session agent reads the same SQLite file and runs independently of FastAPI and the browser. It needs a logged-in, running Windows session; it does not use the microphone or a cloud service. The first install creates a per-user Start Menu shortcut with the required `UNBOUND.AI.Reminders` app identity and registers a Scheduled Task at logon. Windows desktop toast identity depends on a Start Menu shortcut carrying an AppUserModelID ([Microsoft setup requirements](https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid)). The install command is explicit and is not run by the application.

From the repository root in PowerShell, use the same Python environment as the backend:

```powershell
python -m pip install -r backend/requirements-windows-notifications.txt
./scripts/reminder-agent.ps1 install
./scripts/reminder-agent.ps1 status
./scripts/reminder-agent.ps1 stop
./scripts/reminder-agent.ps1 start
./scripts/reminder-agent.ps1 uninstall
```

`run` starts the agent in the foreground for troubleshooting. `install` registers the at-logon task and starts it. `uninstall` removes only the task, Start Menu shortcut, and install marker; reminder data and delivery history remain. To use a non-default database, pass `-DatabasePath 'C:\path\to\conversations.sqlite3'` to every management command.

To verify an actual toast on Windows 11, install the agent, create a synthetic reminder due a few minutes ahead through the API or this UI, confirm it, then close the browser and stop FastAPI. Wait one polling interval after its due time. Check Windows Notification Center and run `./scripts/reminder-agent.ps1 status`; the API/UI should show `submitted` only if the toast call returned without error. Remove the synthetic reminder from the reminder UI afterward. This manual test must be performed on the target Windows account because Windows notification policy and app identity are account-specific; mocked tests do not validate OS display.

The agent polls every 60 seconds by default (`UNBOUND_REMINDER_POLL_SECONDS`, minimum 15 seconds). A due all-day reminder is treated as 9:00 AM in its stored IANA timezone (`UNBOUND_ALL_DAY_REMINDER_TIME` changes the wall time). After sleep or downtime, overdue active reminders are submitted one per polling interval, oldest due first; proposed, completed and deleted entries are ineligible. Failed submissions are retried with bounded exponential delay. A stable Windows toast tag reduces duplicate stacking if the process exits between OS submission and SQLite acknowledgment, but no software can atomically commit both an OS toast and a SQLite transaction. Notification buttons are not included.

Notifications cannot appear while Windows is shut down or the user is logged out. They cannot be submitted while the computer is asleep; overdue reminders are picked up after resume. Agent status is based on a Scheduled Task registration marker plus a heartbeat and can lag by a poll interval. Windows notification policy, Focus Assist, disabled app notifications, locked databases, missing optional dependencies, or an unavailable user profile can prevent display. The OS API accepting a toast is not proof that it appeared. The agent stores only notification attempt metadata alongside reminders and does not alter existing reminder status. No real toast is delivered unless the optional dependencies are installed and the user runs the explicit Windows install command.

If no toast appears, confirm the Scheduled Task is `Running` and the heartbeat is fresh, make sure the Start Menu shortcut `UNBOUND Reminders` exists, and check **Settings → System → Notifications** for UNBOUND/notification permissions and **Do not disturb**. The installer does not enable Windows notifications or change Focus Assist settings.

## Optional local wake word

**Current checkout status:** the OpenWakeWord runtime and two feature assets are present, but the trained custom `hey-unbound.onnx` model is absent. The wake API therefore reports unavailable and does not open the microphone. Exact phrase detection, false acceptance/rejection rates, and microphone lifecycle have not been verified on a real device. No bundled wake word or browser continuous recognition is substituted.

The current implementation selects openWakeWord with an opt-in backend-owned microphone lifecycle. It loads a custom ONNX classifier for **Hey UNBOUND** and two local feature-extraction ONNX files. The UI's local start/stop API and event polling stay the same. Detection releases the microphone before the browser's existing one-shot SpeechRecognition begins. Wake activation only starts recognition; it never classifies, creates, or executes a task.

The custom classifier has **not** been trained or installed yet. No held-out metrics or live microphone detection have been measured. See [model/data license notes](../../docs/WAKE_WORD_MODEL_LICENSES.md); the feature asset license has an unresolved upstream-vs-trainer notice discrepancy, so commercial clearance is not claimed.

### Isolated installation and local training

From the repository root in PowerShell:

```powershell
./scripts/wake-word.ps1 install-runtime
./scripts/wake-word.ps1 install-training
./scripts/wake-word.ps1 prepare
```

This uses `.venv-wake` for openWakeWord and microphone capture, and `.venv-wake-train` for CPU training. Python 3.14.4 installation/import was verified in the dedicated training environment. The trainer wheel is Alpha and currently classifies through Python 3.12, so this local 3.14 success is a smoke check, not upstream support certification. The training dependency downloads are roughly 250–300 MB on this machine. `prepare` copies only the two feature model files bundled with the trainer; it does not download the 17 GB ACAV100M corpus, TTS voices, or other datasets.

The script's 50-positive/50-negative minimum is only a technical guard, not an accuracy target. Upstream openWakeWord recommends several thousand positive samples for a custom phrase; gather substantially more consented examples, plus varied negative speech/noise and disjoint held-out recordings before relying on a model. Add varied rooms, distances, microphones, ordinary speech, noise, and hard negatives. Do not use ACAV100M or its derived features. Training data remains local and the script only uses CPU:

```powershell
# Put training files under data\wake-word\hey-unbound\positive and \negative.
./scripts/wake-word.ps1 train

# Put disjoint evaluation files under \heldout-positive and \heldout-negative.
./scripts/wake-word.ps1 evaluate
./scripts/wake-word.ps1 install-model
```

Training prompts for explicit confirmation that you reviewed recording rights and are not using non-commercial data. The report records held-out recall, false rejection, false positives per hour and threshold when the supplied data permits those metrics. A model that trains or exports successfully is only a candidate until it passes held-out and target-microphone evaluation.

### Start and controls

Set the threshold in the same PowerShell session that launches FastAPI. Start the backend and UI in separate windows:

```powershell
$env:UNBOUND_WAKE_THRESHOLD = "0.65"
& .\.venv-wake\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd experiments/voice-prototype
python -m http.server 8001 --bind 127.0.0.1
```

Open `http://127.0.0.1:8001`; press **Start local “Hey UNBOUND” listening** to request microphone capture. **Mute wake word** and **Disable wake word** stop and release it. No monitoring begins at backend startup or page load. `./scripts/wake-word.ps1 status`, `start`, and `stop` manage the local API detector. Browser event polling renews a five-second UI lease; a lost/closed UI causes the backend to stop and release audio. Run the API with one worker because runtime state and microphone ownership are process-local. Application shutdown also closes the detector.

The microphone is handled locally by `sounddevice`/PortAudio and openWakeWord/ONNX Runtime on CPU. Wake audio is not saved or sent to a cloud service. After detection, browser SpeechRecognition handles the request and may use an online browser service. Push-to-talk stops the backend detector before taking the microphone and wake monitoring resumes only if the user had left it enabled. Browser wake controls remain explicit, and activation does not authorize a task.

### Manual Windows/Edge verification

1. Generate the exact custom model using the local training commands, run `./scripts/wake-word.ps1 validate`, and confirm validation reports `microphone_opened: false`.
2. Start backend/UI and explicitly click Start in Edge. Confirm the visible local listening state.
3. Say “Hey UNBOUND”; verify the backend releases the microphone before browser LISTENING begins. Speak a request, review/edit the transcript, then send. Confirm no task was created by the wake event itself.
4. Test other phrases, varied speech, mute/disable, permission denial, device disconnect/reconnect, push-to-talk handoff, and Stop Speaking while wake mode had been enabled.
5. Use the disjoint held-out evaluation report and record live false activations over a measured duration. Do not invent recall or false-activation rates.

Automated tests use fake detector and audio adapters; they do not prove recognition accuracy, false-positive rate, real microphone capture, Windows permissions, or CPU/RAM use. No live microphone detection has been performed in this implementation.

### Stop Speaking

Stop cancels the native utterance queue, invalidates stale speech callbacks and any pending response's auto-playback token, and keeps response text visible. When wake had been enabled, the detector resumes after playback ends or the user interrupts speech. Node mock coverage does not replace the Edge manual test above.

## Manual reminder API check (PowerShell)

The UI derives the browser's IANA time zone. The API accepts the explicit phrase `tomorrow` and resolves it on the backend in that zone. A newly created proposal is not active until confirmed:

```powershell
$base = "http://127.0.0.1:8000/api"
$reminder = Invoke-RestMethod -Method Post -Uri "$base/reminders" -ContentType "application/json" -Body (@{ title = "Finish project presentation"; due_date = "tomorrow"; timezone = "Asia/Kolkata"; explicitly_confirmed = $false } | ConvertTo-Json)
$reminder.status # proposed
Invoke-RestMethod -Method Post -Uri "$base/reminders/$($reminder.reminder_id)/confirm"
```

The reminder remains stored in the shared SQLite file. To get a toast while the backend and browser are closed, explicitly install the optional Windows agent using the instructions above.

The 503 provider behavior was exercised live through the API with the installed Qwen 3.5 4B model, but the intermittent 503 was not reproduced. One retry is limited to transient connection and 502/503/504 failures. There is no retry after timeout, malformed output, empty output or token-limit exhaustion. Safe server logs include provider/model, request duration, response status and whether an HTTP response arrived; the OpenAI-compatible API does not report whether model generation started before a dropped connection, so that is logged as unconfirmed.
