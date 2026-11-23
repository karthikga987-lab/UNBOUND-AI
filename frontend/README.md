# UNBOUND AI frontend

React/Vite frontend for the UNBOUND task workspace. It includes chat-based task creation, task plans and status, execution timeline, evidence/result panels, approval controls, browser-session Live View components, and an optional voice bubble.

## Requirements

- Node.js 22.12 or newer (Node 20.19+ also works with Vite 8).
- npm 10 or newer.
- A compatible UNBOUND FastAPI backend for real task data. Mock mode is enabled by default.

## Install and run (Windows PowerShell)

From the repository root:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm run dev
```

Open the URL printed by Vite, normally `http://localhost:5173`.

Mock mode (`VITE_USE_MOCK=true`) is enabled by default. It is useful for UI review and does not execute real tools or browser actions.

To use the real task API, start a compatible backend in another PowerShell window. From the repository root, for a backend checkout that has the FastAPI entry point:

```powershell
cd backend
$env:PYTHONPATH = (Resolve-Path ..).Path
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Then set these values in `frontend/.env.local` and restart Vite:

```dotenv
VITE_USE_MOCK=false
VITE_MOCK_AREAS=workflows,documents,integrations
VITE_BACKEND_PROXY_TARGET=http://127.0.0.1:8000
```

The Vite dev server proxies `/api` (including WebSocket upgrades) to the backend, so the browser uses one origin. If the API is served separately, `VITE_API_BASE_URL` can set its base URL; that server must allow the frontend origin through CORS.

## API integration

`src/api/client.js` implements the HTTP client and task API calls. `src/api/index.js` is the single mock-versus-real adapter switch. The real client includes health, task create/read/events/execute/cancel, approval decisions, workflow, document, and integration routes. Set `VITE_MOCK_AREAS` to keep selected endpoint groups on mock data until their backend routes are available.

Real task operations use the shared task API shapes: `TaskResponse`, plan steps, execution events, evidence, and pending approvals. Approval and cancellation controls call the backend; the UI does not locally mark a task complete or verified. Keep frontend enums and field names in `src/api/contracts.js` aligned with `backend/app/schemas/contracts.py`.

## Live View status

The Live View UI is in `src/components/live/`. It consumes browser-session events through the configurable transport in `src/live/` and reuses backend task status, task events, cancellation, and approval endpoints.

The browser event stream, screenshots, and pause/resume endpoints are not implemented by the orchestration backend on this branch. Until a browser backend supplies the event contract in `docs/LIVE_VIEW_CONTRACT.md`, Live View must show its unavailable/reconnecting state; it must not imply that browser automation or screenshots are active. Pause and resume remain disabled unless the backend advertises those capabilities.

## Voice mode

The floating voice bubble is optional and uses browser speech APIs when available. Browser support and speech processing behavior vary; text chat remains available. Voice requests follow the same task API path, and approval controls remain explicit UI actions.

## Commands

Run from `frontend/`:

```powershell
npm test       # Vitest component and API-client tests
npm run lint   # ESLint
npm run build  # Production build in dist/
npm run preview
```

`package-lock.json` is committed; use `npm ci` for reproducible installation. No frontend command starts or installs the backend.
