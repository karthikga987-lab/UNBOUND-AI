# Browserless Cloud Demo and Live Browser View

The Browserless provider is an explicit demo execution backend in the existing
FastAPI browser tools and TaskExecutor. It does not replace the Ubuntu worker,
does not change the Windows/Ubuntu containment gates, and never falls back to
local Chromium when selected.

## Demo policy and limits

- `UNBOUND_BROWSER_PROVIDER=browserless` selects Browserless Cloud.
- The configured exact-host list in `BROWSERLESS_ALLOWED_HOSTS` is the baseline
  permission set (default `example.com`). A planned browser task may request
  additional exact DNS hostnames. The plan review shows baseline hosts and new
  hosts separately; only the explicit **Run plan** action grants the displayed
  hosts to that task. The authorization decision and timestamp are recorded on
  the task. Grants are bound to the active task and step, and expire on task
  completion, failure, cancellation, or backend restart. A later task starts
  with only its own requested hosts and the baseline.
- Only HTTPS on port 443 is accepted. IP literals, URL credentials, malformed
  hosts, non-public or mixed public/private DNS answers, and redirects or
  subresources outside the task's exact host set are rejected. Each navigation
  and intercepted request is revalidated.
- Playwright uses a native `chromium.connect()` WebSocket connection. The token
  is appended only by the backend and is not returned in health, events, errors,
  or frontend configuration.
- Service workers are blocked, WebSocket page requests are closed, and the page
  WebRTC peer-connection APIs are disabled before page scripts run.
- Requests are guarded in the cloud browser context. DNS is checked from the
  backend before a request, but Browserless resolves the destination itself;
  this does not pin the cloud browser's socket to the backend's checked address
  and is not a defense against a compromised cloud provider or cross-resolver
  DNS rebinding.
- The domain grant is implemented by UNBOUND's per-task Playwright request
  interceptor. This integration does not configure or verify an account-level
  Browserless network allowlist. It cannot pin Browserless's network socket to
  the backend resolver result; the UI and health endpoint must not describe this
  as provider-level or OS-level egress containment.
- These page/request controls do not provide process-level network isolation:
  Browserless controls the remote browser process and its UDP/network stack.
  They are demo safeguards only, not a substitute for host/guest OS enforcement.
- Therefore Browserless Cloud is a separately identified hackathon demo with
  application-level restrictions. It is **not** proof of guest containment,
  Windows gateway egress enforcement, WFP policy, or public-browsing readiness
  under the self-hosted D/E/I gates.

## Start the isolated local demo stack

From Windows PowerShell at the repository root:

```powershell
.\scripts\browser-security\start-browserless-demo.ps1
```

The script prompts for the API token with hidden input and for a WSS endpoint
(default `wss://production-sfo.browserless.io/chromium/playwright`). It starts
a new loopback-only FastAPI process on port 8004 and Vite on port 5177, using a
new private local data directory. It checks the ports first and leaves existing
services, databases, and the Local Demo untouched. The token is placed only in
the backend process environment and is removed from the PowerShell process
environment after launch. Do not place the token in command-line arguments,
frontend variables, `.env` files, logs, or source control.

To restart this demo against its existing private task and conversation data,
pass that exact directory with `-RuntimeDirectory` along with the desired ports.
The script validates the directory location, owner, protected ACL, reparse-point
state, and previous process manifests. It refuses to reuse the directory while
a recorded demo process is still active. It reuses the existing task database,
conversation database, documents, and workflows, and writes new uniquely named
logs and process metadata so previous run files are not overwritten. For
example:

```powershell
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'UNBOUND'
$runtimeMatches = @(Get-ChildItem -LiteralPath $runtimeRoot -Directory -Filter 'browserless-demo-*' | Where-Object {
  $manifestPath = Join-Path $_.FullName 'processes.json'
  if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $manifest.backend_port -eq 8005 -and $manifest.frontend_port -eq 5178
})
if ($runtimeMatches.Count -ne 1) { throw 'Expected exactly one existing 8005/5178 Browserless data directory.' }
.\scripts\browser-security\start-browserless-demo.ps1 `
  -BackendPort 8005 `
  -FrontendPort 5178 `
  -RuntimeDirectory $runtimeMatches[0].FullName
```

Health at `http://127.0.0.1:8004/api/health` reports Browserless as configured
before a session is tested, and reports connection availability only while an
actual session is connected. The root self-hosted public-browsing gate remains
false.

## Live execution

Open the configured frontend (the current 8005/5178 demo uses
`http://127.0.0.1:5178`), submit a browser task, review the exact hosts in the
generated plan, then select **Run plan**. Running a
plan opens the Live Browser view. It polls the task-scoped backend event route
and displays actual backend stages, the current URL and title, and a bounded PNG
captured from the active Playwright page after navigation/click actions. The
browser session closes at task termination or application shutdown. The final
task result retains the verified observation and screenshot SHA-256 evidence.

The live event buffer is transient and bounded. It is associated with one task
ID and opaque browser session ID; it is not a control channel. Browserless
sessions and screenshots remain subject to the task's existing approvals and
backend evidence verification.

## Shutdown

Use the recorded backend/frontend PIDs from the private `processes.json` file
under `%LOCALAPPDATA%\UNBOUND\browserless-demo-*` to close only this demo
instance. Confirm each process path and PID before terminating it. The main
backend and frontend used for Local Demo are separate and must remain running.

## Test boundaries

Unit and integration tests use mock Playwright transports, synthetic DNS
answers, and the existing local synthetic fixture. They do not test Browserless
credentials, a real cloud browser connection, or real public navigation. A
live public task is accepted only after the demo token is entered locally and
an actual task succeeds through the React page.
