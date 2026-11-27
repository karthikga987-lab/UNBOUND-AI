// Pure state for a live browser session. Everything here is derived from real backend events only;
// nothing is invented or animated independently of them.
import { compareEvents, LiveEventType, normalizeEvent } from './contract'

export const MAX_TIMELINE_EVENTS = 500

const EXECUTION_BY_EVENT = {
  [LiveEventType.EXECUTION_PAUSED]: 'paused',
  [LiveEventType.EXECUTION_RESUMED]: 'running',
  [LiveEventType.EXECUTION_CANCELLED]: 'cancelled',
  [LiveEventType.EXECUTION_FAILED]: 'failed',
  [LiveEventType.SESSION_STARTED]: 'running',
  [LiveEventType.SESSION_ENDED]: 'ended',
}

export function createLiveState() {
  return {
    events: [],
    seen: {},
    arrivals: 0,
    sessionId: null,
    screenshot: null,
    cursor: null,
    lastClick: null,
    typing: null,
    target: null,
    page: null,
    currentAction: null,
    execution: null,
    verification: null,
    error: null,
    executionStage: null,
    pageTitle: null,
    provider: null,
    lastEventAt: null,
  }
}

// Only let an event overwrite a piece of state if it is not older than what is already shown,
// so late or replayed events never move the cursor or screen backwards.
const isNewer = (current, time) => !current || time >= current.time

function applyEvent(prev, ev) {
  const next = { ...prev }
  next.lastEventAt = Math.max(prev.lastEventAt ?? 0, ev.time)
  next.sessionId = prev.sessionId ?? ev.session_id
  if (ev.metadata?.stage && isNewer(prev.executionStage, ev.time)) {
    next.executionStage = { stage: ev.metadata.stage, description: ev.description, time: ev.time }
  }
  if (ev.metadata?.page_title && isNewer(prev.pageTitle, ev.time)) {
    next.pageTitle = { title: ev.metadata.page_title, time: ev.time }
  }
  if (ev.metadata?.provider && isNewer(prev.provider, ev.time)) {
    next.provider = { name: ev.metadata.provider, time: ev.time }
  }

  if (ev.screenshot && (!prev.screenshot || ev.screenshot.captured_at >= prev.screenshot.captured_at)) {
    next.screenshot = { ...ev.screenshot, event_id: ev.event_id }
  }
  if (ev.cursor && isNewer(prev.cursor, ev.time)) next.cursor = { ...ev.cursor, time: ev.time }
  if (ev.target && isNewer(prev.target, ev.time)) next.target = { ...ev.target, time: ev.time }
  if (ev.url && isNewer(prev.page, ev.time)) next.page = { url: ev.url, time: ev.time }

  switch (ev.event_type) {
    case LiveEventType.MOUSE_CLICK:
      if (ev.cursor && isNewer(prev.lastClick, ev.time)) next.lastClick = { ...ev.cursor, time: ev.time, event_id: ev.event_id }
      break
    case LiveEventType.KEYBOARD_INPUT:
      if (ev.input && isNewer(prev.typing, ev.time)) next.typing = { ...ev.input, time: ev.time }
      break
    case LiveEventType.ACTION_STARTED:
    case LiveEventType.ACTION_COMPLETED:
      if (isNewer(prev.currentAction, ev.time)) {
        next.currentAction = { description: ev.description, status: ev.event_type === LiveEventType.ACTION_STARTED ? 'running' : 'completed', step_id: ev.step_id, time: ev.time }
      }
      break
    case LiveEventType.VERIFICATION_COMPLETED:
      if (ev.verification && isNewer(prev.verification, ev.time)) next.verification = { ...ev.verification, time: ev.time }
      break
    case LiveEventType.EXECUTION_FAILED:
      next.error = ev.description
      break
    default:
      break
  }

  const execution = EXECUTION_BY_EVENT[ev.event_type]
  if (execution && isNewer(prev.execution, ev.time)) next.execution = { state: execution, time: ev.time }
  return next
}

function applyEvents(state, rawEvents) {
  const seen = { ...state.seen }
  const added = []
  let arrival = state.arrivals
  for (const raw of Array.isArray(rawEvents) ? rawEvents : []) {
    const ev = normalizeEvent(raw, arrival++)
    if (!ev || seen[ev.event_id]) continue // invalid or duplicate (e.g. replayed after a reconnect)
    seen[ev.event_id] = true
    added.push(ev)
  }
  if (!added.length) return state

  added.sort(compareEvents)
  let next = { ...state, seen, arrivals: arrival }
  for (const ev of added) next = applyEvent(next, ev)
  const events = [...state.events, ...added].sort(compareEvents)
  next.events = events.length > MAX_TIMELINE_EVENTS ? events.slice(-MAX_TIMELINE_EVENTS) : events
  return next
}

export function liveReducer(state, action) {
  switch (action.type) {
    case 'events':
      return applyEvents(state, action.events)
    case 'reset':
      return createLiveState()
    default:
      return state
  }
}

/**
 * One user-facing status. Terminal states come ONLY from the backend task status, never from
 * the absence of errors and never from a live event alone.
 */
export function deriveDisplayStatus({ taskStatus, execution, cancelling = false }) {
  if (taskStatus === 'cancelled') return 'cancelled'
  if (taskStatus === 'completed') return 'completed'
  if (taskStatus === 'failed') return 'failed'
  if (cancelling) return 'cancelling'
  if (taskStatus === 'needs_attention') return 'needs_attention'
  if (taskStatus === 'awaiting_approval') return 'waiting_for_approval'
  if (execution === 'paused') return 'paused'
  if (taskStatus === 'executing' || taskStatus === 'verifying') return 'running'
  return 'initializing'
}

export const DISPLAY_STATUS_LABEL = {
  initializing: 'Initializing',
  running: 'Running',
  waiting_for_approval: 'Waiting for approval',
  paused: 'Paused',
  cancelling: 'Cancelling…',
  cancelled: 'Cancelled',
  completed: 'Completed',
  failed: 'Failed',
  needs_attention: 'Needs attention',
}
