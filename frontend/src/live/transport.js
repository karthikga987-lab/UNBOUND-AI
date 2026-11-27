// Real-time transport for the live view: WebSocket, Server-Sent Events, or polling.
// The URL is configured, never hardcoded (see browserSessionService.js).

export const ConnectionStatus = Object.freeze({
  UNAVAILABLE: 'unavailable',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  RECONNECTING: 'reconnecting',
  DISCONNECTED: 'disconnected',
  ENDED: 'ended',
})

export const MIN_POLL_MS = 1000
export const MAX_RETRIES = 8

/** Fills {task_id} / {after} placeholders. `after` = last received sequence, so the server can replay only newer events. */
export function buildUrl(template, params) {
  return String(template).replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(params[key] ?? ''))
}

/** Accepts a single event, an array of events, or {events: [...]}. */
export function extractEvents(payload) {
  if (Array.isArray(payload)) return payload
  if (payload && Array.isArray(payload.events)) return payload.events
  if (payload && typeof payload === 'object' && payload.event_id != null) return [payload]
  return []
}

export const backoffDelay = (attempt, base = 1000, max = 15000) => Math.min(max, base * 2 ** attempt)

export function openLiveConnection({ transport, url, taskId, onEvents, onStatus, pollMs = 1500, maxRetries = MAX_RETRIES, deps = {} }) {
  const WebSocketImpl = deps.WebSocket ?? globalThis.WebSocket
  const EventSourceImpl = deps.EventSource ?? globalThis.EventSource
  const fetchImpl = deps.fetch ?? ((...args) => globalThis.fetch(...args))
  const interval = Math.max(MIN_POLL_MS, Number(pollMs) || MIN_POLL_MS)

  let closed = false
  let ended = false
  let attempt = 0
  let timer = null
  let socket = null
  let lastSequence = null
  let current = null

  const setStatus = (status) => {
    if (closed || status === current) return
    current = status
    onStatus(status)
  }
  const currentUrl = () => buildUrl(url, { task_id: taskId, after: lastSequence ?? 0 })

  function detachSocket() {
    if (!socket) return
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    try {
      socket.close()
    } catch {
      /* already closed */
    }
    socket = null
  }

  function close() {
    closed = true
    clearTimeout(timer)
    timer = null
    detachSocket()
  }

  function deliver(payload) {
    const events = extractEvents(payload)
    if (!events.length) return
    for (const ev of events) {
      if (Number.isInteger(ev?.sequence)) lastSequence = Math.max(lastSequence ?? ev.sequence, ev.sequence)
      if (['session_ended', 'completed', 'failed', 'cancelled'].includes(ev?.event_type)) ended = true
    }
    onEvents(events)
    if (ended) {
      setStatus(ConnectionStatus.ENDED)
      close()
    }
  }

  function scheduleReconnect() {
    if (closed || ended) return
    if (attempt >= maxRetries) {
      setStatus(ConnectionStatus.DISCONNECTED)
      return
    }
    setStatus(ConnectionStatus.RECONNECTING)
    timer = setTimeout(connect, backoffDelay(attempt++))
  }

  async function poll() {
    try {
      const response = await fetchImpl(currentUrl(), { headers: { Accept: 'application/json' } })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const payload = await response.json()
      if (closed) return
      attempt = 0
      setStatus(ConnectionStatus.CONNECTED)
      deliver(payload)
      if (!closed && !ended) timer = setTimeout(poll, interval)
    } catch {
      if (!closed) scheduleReconnect()
    }
  }

  function connectStream(Impl) {
    socket = new Impl(currentUrl())
    socket.onopen = () => {
      attempt = 0
      setStatus(ConnectionStatus.CONNECTED)
    }
    socket.onmessage = (message) => {
      try {
        deliver(JSON.parse(message.data))
      } catch {
        /* ignore malformed frames instead of crashing the view */
      }
    }
    socket.onerror = () => {
      // EventSource retries on its own; we take over so backoff and status stay consistent.
      if (Impl === EventSourceImpl) {
        detachSocket()
        scheduleReconnect()
      }
    }
    socket.onclose = () => {
      socket = null
      scheduleReconnect()
    }
  }

  function connect() {
    if (closed) return
    timer = null
    if (transport === 'websocket' && WebSocketImpl) connectStream(WebSocketImpl)
    else if (transport === 'sse' && EventSourceImpl) connectStream(EventSourceImpl)
    else if (transport === 'polling') poll()
    else setStatus(ConnectionStatus.UNAVAILABLE)
  }

  function retry() {
    if (closed) return
    clearTimeout(timer)
    detachSocket()
    attempt = 0
    setStatus(ConnectionStatus.CONNECTING)
    connect()
  }

  setStatus(ConnectionStatus.CONNECTING)
  connect()
  return { close, retry }
}
