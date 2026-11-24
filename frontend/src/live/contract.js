// PROPOSED browser live-view event contract, version 1.0.
// No backend implements this yet. Member 2 (browser engine) owns the server side.
// Full description: frontend/docs/LIVE_VIEW_CONTRACT.md

export const LIVE_CONTRACT_VERSION = '1.0'

export const LiveEventType = Object.freeze({
  SESSION_STARTED: 'session_started',
  SCREENSHOT_UPDATED: 'screenshot_updated',
  NAVIGATION: 'navigation',
  MOUSE_MOVE: 'mouse_move',
  MOUSE_CLICK: 'mouse_click',
  KEYBOARD_INPUT: 'keyboard_input',
  PAGE_LOADED: 'page_loaded',
  ACTION_STARTED: 'action_started',
  ACTION_COMPLETED: 'action_completed',
  APPROVAL_REQUIRED: 'approval_required',
  EXECUTION_PAUSED: 'execution_paused',
  EXECUTION_RESUMED: 'execution_resumed',
  EXECUTION_CANCELLED: 'execution_cancelled',
  VERIFICATION_COMPLETED: 'verification_completed',
  EXECUTION_FAILED: 'execution_failed',
  SESSION_ENDED: 'session_ended',
})

export const MASK = '••••••'
const SENSITIVE_KEY = /(pass(word|code|phrase)?|pwd|secret|token|api[_-]?key|auth|cookie|session[_-]?key|card|cvv|cvc|iban|account[_-]?number|ssn|otp|\bpin\b)/i
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])
const MAX_TEXT = 300

export const isSensitiveKey = (key) => SENSITIVE_KEY.test(String(key ?? ''))

// Defence in depth: the backend must already redact secrets, but never render them if it does not.
export function maskSensitive(value, depth = 0) {
  if (depth > 6 || value == null) return value
  if (Array.isArray(value)) return value.map((v) => maskSensitive(v, depth + 1))
  if (typeof value !== 'object') return value
  return Object.fromEntries(
    Object.entries(value).map(([k, v]) => [k, isSensitiveKey(k) ? MASK : maskSensitive(v, depth + 1)]),
  )
}

const text = (v) => (typeof v === 'string' && v.trim() ? v.trim().slice(0, MAX_TEXT) : null)
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const positive = (v) => (num(v) != null && v > 0 ? v : null)

export function safeHttpUrl(value) {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : null
  } catch {
    return null
  }
}

function screenshotSrc(shot) {
  if (typeof shot.data === 'string' && shot.data) {
    const data = shot.data.replace(/\s/g, '')
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(data)) return null
    const mime = IMAGE_TYPES.has(shot.mime_type) ? shot.mime_type : 'image/png'
    return `data:${mime};base64,${data}`
  }
  if (typeof shot.url === 'string') {
    if (shot.url.startsWith('/') && !shot.url.startsWith('//')) return shot.url // same origin, via /api proxy
    return safeHttpUrl(shot.url)
  }
  return null
}

function normalizeScreenshot(shot, eventTime) {
  if (!shot || typeof shot !== 'object') return null
  const src = screenshotSrc(shot)
  if (!src) return null
  const captured = Date.parse(shot.captured_at ?? '')
  return {
    src,
    width: positive(shot.width),
    height: positive(shot.height),
    captured_at: Number.isNaN(captured) ? eventTime : captured,
  }
}

function normalizePoint(point) {
  if (!point || typeof point !== 'object') return null
  const x = num(point.x)
  const y = num(point.y)
  return x != null && y != null && x >= 0 && y >= 0 ? { x, y } : null
}

function normalizeRect(rect) {
  if (!rect || typeof rect !== 'object') return null
  const x = num(rect.x)
  const y = num(rect.y)
  const width = positive(rect.width)
  const height = positive(rect.height)
  return x != null && y != null && width && height ? { x, y, width, height } : null
}

function normalizeTarget(target) {
  if (!target || typeof target !== 'object') return null
  const out = { label: text(target.label), role: text(target.role), selector: text(target.selector), bbox: normalizeRect(target.bbox) }
  return out.label || out.role || out.selector || out.bbox ? out : null
}

function normalizeInput(input) {
  if (!input || typeof input !== 'object') return null
  const fieldLabel = text(input.field_label) ?? text(input.field_name)
  const sensitive = input.masked === true || isSensitiveKey(input.field_name) || isSensitiveKey(input.field_label) || input.input_type === 'password'
  const preview = text(input.text_preview)
  return { field_label: fieldLabel, text_preview: sensitive ? MASK : preview, masked: sensitive }
}

const READABLE = {
  session_started: 'Browser session started',
  screenshot_updated: 'Screen updated',
  navigation: 'Navigated',
  mouse_move: 'Mouse moved',
  mouse_click: 'Clicked',
  keyboard_input: 'Typed text',
  page_loaded: 'Page loaded',
  action_started: 'Action started',
  action_completed: 'Action completed',
  approval_required: 'Waiting for user approval',
  execution_paused: 'Execution paused',
  execution_resumed: 'Execution resumed',
  execution_cancelled: 'Execution cancelled',
  verification_completed: 'Verification completed',
  execution_failed: 'Execution failed',
  session_ended: 'Browser session ended',
}

export const readableEventType = (type) => READABLE[type] ?? String(type).replaceAll(/[._]/g, ' ')

/** Validates and sanitises one raw event. Returns null for anything that does not meet the contract. */
export function normalizeEvent(raw, arrival = 0) {
  if (!raw || typeof raw !== 'object') return null
  const eventId = raw.event_id != null ? String(raw.event_id) : null
  const eventType = text(raw.event_type)
  const time = Date.parse(raw.timestamp ?? '')
  if (!eventId || !eventType || Number.isNaN(time)) return null
  const url = safeHttpUrl(raw.url)
  const description = text(raw.description) ?? (url ? `${readableEventType(eventType)}: ${url}` : readableEventType(eventType))
  return {
    contract_version: text(raw.contract_version),
    session_id: raw.session_id != null ? String(raw.session_id) : null,
    task_id: raw.task_id != null ? String(raw.task_id) : null,
    event_id: eventId,
    sequence: Number.isInteger(raw.sequence) ? raw.sequence : null,
    timestamp: new Date(time).toISOString(),
    time,
    arrival,
    event_type: eventType,
    description,
    step_id: text(raw.step_id),
    url,
    screenshot: normalizeScreenshot(raw.screenshot, time),
    cursor: normalizePoint(raw.cursor),
    target: normalizeTarget(raw.target),
    input: normalizeInput(raw.input),
    action_status: text(raw.action_status),
    verification:
      raw.verification && typeof raw.verification === 'object'
        ? { status: text(raw.verification.status) ?? 'unknown', detail: text(raw.verification.detail) }
        : null,
    metadata: raw.metadata && typeof raw.metadata === 'object' ? maskSensitive(raw.metadata) : null,
  }
}

/** Order by per-session sequence when both have one, otherwise by timestamp, then arrival. */
export function compareEvents(a, b) {
  if (a.sequence != null && b.sequence != null && a.session_id === b.session_id && a.sequence !== b.sequence) {
    return a.sequence - b.sequence
  }
  if (a.time !== b.time) return a.time - b.time
  return a.arrival - b.arrival
}