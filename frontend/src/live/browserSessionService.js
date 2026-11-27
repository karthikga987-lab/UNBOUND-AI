// Browser session service abstraction for the Live View.
// The backend provides task-scoped browser-event polling. Pause/resume remain
// unavailable unless a backend explicitly configures those control endpoints.
// See frontend/docs/LIVE_VIEW_CONTRACT.md for the supported event contract.
import { buildUrl, ConnectionStatus, openLiveConnection } from './transport'

const TRANSPORTS = new Set(['websocket', 'sse', 'polling', 'none'])

export function getLiveConfig(env = import.meta.env) {
  const apiBase = String(env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
  const configuredTransport = env.VITE_LIVE_TRANSPORT
  return {
    transport: configuredTransport === undefined ? 'polling' : TRANSPORTS.has(configuredTransport) ? configuredTransport : 'none',
    eventsUrl: env.VITE_LIVE_EVENTS_URL ?? `${apiBase}/api/tasks/{task_id}/browser-events?after={after}`,
    pollMs: Number(env.VITE_LIVE_POLL_MS) || 1500,
    pauseUrl: env.VITE_LIVE_PAUSE_URL ?? '',
    resumeUrl: env.VITE_LIVE_RESUME_URL ?? '',
  }
}

function parseJson(text) {
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

function errorMessage(data, status) {
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (typeof detail?.message === 'string') return detail.message
  return `Request failed (${status})`
}

export function createBrowserSessionService(config, deps = {}) {
  const capabilities = Object.freeze({
    liveEvents: config.transport !== 'none' && Boolean(config.eventsUrl),
    pause: Boolean(config.pauseUrl),
    resume: Boolean(config.resumeUrl),
  })
  const fetchImpl = deps.fetch ?? ((...args) => globalThis.fetch(...args))

  async function control(template, taskId) {
    const response = await fetchImpl(buildUrl(template, { task_id: taskId }), { method: 'POST', headers: { Accept: 'application/json' } })
    const data = parseJson(await response.text())
    if (!response.ok) throw new Error(errorMessage(data, response.status))
    return data
  }

  return {
    capabilities,
    connect(taskId, { onEvents, onStatus }) {
      if (!capabilities.liveEvents) {
        onStatus(ConnectionStatus.UNAVAILABLE)
        return { close() {}, retry() {} }
      }
      return openLiveConnection({ transport: config.transport, url: config.eventsUrl, taskId, pollMs: config.pollMs, onEvents, onStatus, deps })
    },
    // The returned body is shown as-is; success is never assumed. Expected: {"execution_state": "paused" | "running"}.
    pause: (taskId) => (capabilities.pause ? control(config.pauseUrl, taskId) : Promise.reject(new Error('Pause is not supported by the backend yet.'))),
    resume: (taskId) => (capabilities.resume ? control(config.resumeUrl, taskId) : Promise.reject(new Error('Resume is not supported by the backend yet.'))),
  }
}

export const browserSessionService = createBrowserSessionService(getLiveConfig())
