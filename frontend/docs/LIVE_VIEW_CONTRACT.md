# Live View browser event contract (v1.0)

**Status: PARTIALLY IMPLEMENTED.** The FastAPI backend exposes task-scoped polling at
`GET /api/tasks/{task_id}/browser-events?after={sequence}` and publishes bounded events through
`BrowserLiveEventStore`. Current events may include a sanitized URL, page title, provider name, and the latest bounded
PNG screenshot as base64 with a SHA-256 hash. This document retains the fuller v1.0 shape for compatible extensions;
cursor/target/input overlays, WebSocket/SSE delivery, and pause/resume controls are not currently implemented.

Nothing in the frontend hardcodes these endpoints. Every URL is provided through `frontend/.env.local` (see the end of this document).

## What already exists and is reused

| Need | Existing backend API (Member 3) | Used by Live View |
|---|---|---|
| Task, plan, current step, status | `GET /api/tasks/{task_id}` → `TaskResponse` | Task status panel |
| Task activity | `GET /api/tasks/{task_id}/events` → `ExecutionEvent[]` | Merged into the activity timeline |
| Stop | `POST /api/tasks/{task_id}/cancel` | **Stop** button |
| Approvals | `POST /api/tasks/{task_id}/approvals/{approval_id}` `{"decision":"APPROVED"\|"REJECTED"}`, then `POST /execute` | Review panel |

Approvals, cancellation, execution and safety stay entirely in the orchestrator. The frontend never approves,
executes or marks anything complete on its own.

## Current limits

The current transport is JSON polling. Optional **pause** and **resume** endpoints are not implemented and their
controls stay disabled. Events are transient and bounded in memory; reconnect can retrieve only events retained for
the current backend process.

## Event shape

Each message is one event object, an array of events, or `{"events": [...]}`.

```json
{
  "contract_version": "1.0",
  "session_id": "b7d0…",
  "task_id": "5f3c…",
  "event_id": "unique-per-event",
  "sequence": 42,
  "timestamp": "2026-11-23T10:00:05.120Z",
  "event_type": "mouse_click",
  "description": "Clicked the \"Search\" button",
  "step_id": "step_2",
  "url": "https://example.com/search",
  "screenshot": { "url": "/api/browser/sessions/b7d0…/frames/42.jpg", "width": 1280, "height": 800, "captured_at": "2026-11-23T10:00:05.100Z" },
  "cursor": { "x": 640, "y": 412 },
  "target": { "label": "Search", "role": "button", "selector": "#search", "bbox": { "x": 600, "y": 395, "width": 90, "height": 36 } },
  "input": { "field_label": "Destination", "text_preview": "Goa", "masked": false },
  "action_status": "completed",
  "verification": { "status": "passed", "detail": "Results list is visible" },
  "metadata": {}
}
```

| Field | Required | Notes |
|---|---|---|
| `event_id` | yes | Unique. Used to drop duplicates, so replaying events after a reconnect is safe. |
| `event_type` | yes | See the list below. Unknown types are still shown in the timeline. |
| `timestamp` | yes | ISO-8601 UTC. |
| `sequence` | recommended | Monotonic integer per `session_id`. Used for ordering and for `{after}` on reconnect. |
| `session_id`, `task_id` | recommended | `task_id` must be the orchestrator task UUID. |
| `description` | recommended | Human-readable; a default is derived from the type if missing. |
| `screenshot` | when available | Either `url` (http(s) or same-origin path) or base64 `data` + `mime_type` (`image/png`, `image/jpeg`, `image/webp`). **`width` and `height` are required for overlays to be drawn.** |
| `cursor` | for mouse events | Pixel coordinates in the screenshot's coordinate space. |
| `target` | optional | Element being interacted with. `bbox` in screenshot pixels draws a highlight box. |
| `input` | for `keyboard_input` | **Never send secrets.** Set `masked: true` for password/payment/OTP fields; the frontend also masks sensitive-looking field names. |
| `verification` | for `verification_completed` | `status`: `passed` / `failed` / `pending`. |
| `metadata` | optional | Free-form; keys that look like secrets are masked before display. |

### Event types

`session_started`, `screenshot_updated`, `navigation`, `mouse_move`, `mouse_click`, `keyboard_input`, `page_loaded`,
`action_started`, `action_completed`, `approval_required`, `execution_paused`, `execution_resumed`,
`execution_cancelled`, `verification_completed`, `execution_failed`, `session_ended`.

`session_ended` tells the frontend to close the connection and stop reconnecting.

## Transport

| Transport | Frontend behaviour |
|---|---|
| WebSocket | Opens `VITE_LIVE_EVENTS_URL`. Reconnects with exponential backoff (1s → 15s, 8 attempts), then shows **Disconnected** with a Reconnect button. |
| SSE | Same URL semantics; each `data:` line is a JSON event or array. |
| Polling | `GET` every `VITE_LIVE_POLL_MS` (minimum 1000 ms). Response: an array or `{"events": [...]}`. |

URL placeholders: `{task_id}` and `{after}` (the last `sequence` received, empty on first connect). The server should
return or replay only events after `{after}`; duplicates are harmless either way.

Stale frames: a screenshot older than the one shown (by `captured_at`) is ignored. If no new frame arrives for 15 s
while connected, the viewer labels the frame as stale.

## Pause / resume (optional)

`POST` to the configured URL. Respond with `{"execution_state": "paused"}` or `{"execution_state": "running"}`
(or any body plus a later `execution_paused` / `execution_resumed` event). Errors should use the existing format
`{"detail": {"code": "...", "message": "..."}}`; the message is shown to the user. The frontend never shows a pause or
resume as done until the backend confirms it.

## Privacy and retention

- Redact passwords, payment details, tokens and OTPs **before** they reach the stream or a screenshot where possible.
- The frontend keeps events and frames only in memory for the open Live View (at most 500 timeline events) and discards
  them when the view closes. It stores nothing. Any server-side screenshot retention needs an explicit policy.
- Avoid sending local filesystem paths; send URLs served through the API.
- Page content is untrusted: the frontend renders it as text or an image only, never as HTML.

## Connecting Member 2's backend

1. Expose the stream (and optionally pause/resume) under `/api/...` on the FastAPI app.
2. Create `frontend/.env.local`, for example:

   ```
   VITE_USE_MOCK=false
   VITE_LIVE_TRANSPORT=websocket
   VITE_LIVE_EVENTS_URL=ws://localhost:5173/api/browser/sessions/{task_id}/events?after={after}
   VITE_LIVE_PAUSE_URL=/api/browser/sessions/{task_id}/pause
   VITE_LIVE_RESUME_URL=/api/browser/sessions/{task_id}/resume
   ```

   The paths above are examples; use whatever the backend actually exposes. The Vite dev server proxies `/api`
   (including WebSockets) to `http://127.0.0.1:8000`, so no CORS setup is needed.
3. Restart `npm run dev`, start a task that uses the browser, and open **Live View**.
4. Check: frames appear, the cursor/click/typing overlays line up with the page, the activity feed is in order,
   pulling the network briefly shows Reconnecting and then recovers, and Stop ends in **Cancelled** only once the task API says so.

## Verification levels (implementation status)

1. Frontend interface and task-scoped polling client: **implemented**.
2. Event store, polling route, transport, and component behavior: **covered by automated tests**.
3. Browser provider integration: **implemented for configured providers; provider-specific runtime status is reported separately**.
4. Independent OS-level network containment: **not established by this Live View contract or its tests**.
