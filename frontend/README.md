# UNBOUND AI frontend

React/Vite frontend for the UNBOUND task workspace. It includes chat-based task creation, task plans and status, execution timeline, evidence/result panels, approval controls, browser-session Live View components, and an optional voice bubble.

## Requirements

- Node.js 22.12 or newer (Node 20.19+ also works with Vite 8).
- npm 10 or newer.
- A compatible UNBOUND FastAPI backend for real task data. The real backend is selected by default; mock mode must be explicitly enabled.

## Install and run (Windows PowerShell)

From the repository root:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm run dev
```

Open the URL printed by Vite, normally `http://localhost:5173`.

Mock mode (`VITE_USE_MOCK=true`) is opt-in for UI review only. It does not execute real tools or browser actions. When an area is selected through `VITE_MOCK_AREAS`, the header displays those areas as mock data. The checked-in `.env.example` leaves mock areas empty. The existing ignored `.env.local` is user configuration and is not overwritten by these instructions.

For the standard Local Demo development profile, start the backend in another PowerShell window. From the repository root:

```powershell
cd backend
$env:PYTHONPATH = (Resolve-Path ..).Path
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Then set these values in `frontend/.env.local` and restart Vite:

```dotenv
VITE_USE_MOCK=false
VITE_MOCK_AREAS=
VITE_BACKEND_PROXY_TARGET=http://127.0.0.1:8001
```

An isolated-worker profile must use its own explicit backend and frontend ports and a separate data directory; it is not a Local Demo fallback. Keep its proxy target explicitly configured by the reviewed launch script.

The Vite dev server proxies `/api` (including WebSocket upgrades) to the backend, so the browser uses one origin. If the API is served separately, `VITE_API_BASE_URL` can set its base URL; that server must allow the frontend origin through CORS.

## API integration

`src/api/client.js` implements the HTTP client and task API calls. `src/api/index.js` is the single mock-versus-real adapter switch. The real client includes health, task create/read/events/execute/cancel, approval decisions, workflow, document, and integration routes. Document uploads in this checkout support PDF, DOCX, and TXT. Browser workflow and integration routes are not implemented on this branch; the real client reports those backend errors instead of simulating success. Keep mock responses limited to an explicitly labelled UI demo.

Real task operations use the shared task API shapes: `TaskResponse`, plan steps, execution events, evidence, and pending approvals. Approval and cancellation controls call the backend; the UI does not locally mark a task complete or verified. Keep frontend enums and field names in `src/api/contracts.js` aligned with `backend/app/schemas/contracts.py`.

## Live View status

The Live View UI is in `src/components/live/`. It consumes browser-session events through the configurable transport in `src/live/` and reuses backend task status, task events, cancellation, and approval endpoints.

The backend exposes task-scoped browser events at `GET /api/tasks/{task_id}/browser-events`; the Live View polls this endpoint by default and displays only events published by the active browser runtime. Screenshot frames are carried as bounded PNG data when available. WebSocket/SSE transport and pause/resume controls are not currently provided by the backend, so those controls remain unavailable unless explicitly configured. An empty event stream is not evidence that a browser action occurred.

## Voice mode

The floating voice bubble is optional and uses browser speech APIs when available. Browser support and speech processing behavior vary; text chat remains available. React voice commands can act on an existing prepared task, but the React app does not yet provide the persistent conversation-session UI; the separate `experiments/voice-prototype/` uses the conversation APIs. Voice task preparation remains separate from task execution, and approval controls remain explicit UI actions.

## Commands

Run from `frontend/`:

```powershell
npm test       # Vitest component and API-client tests
npm run lint   # ESLint
npm run build  # Production build in dist/
npm run preview
```

`package-lock.json` is committed; use `npm ci` for reproducible installation. No frontend command starts or installs the backend.
