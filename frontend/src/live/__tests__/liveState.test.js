// Unit tests with hand-written events (mocked data). They do NOT prove a real browser backend works.
import { describe, expect, it } from 'vitest'
import { MASK, normalizeEvent } from '../contract'
import { createLiveState, deriveDisplayStatus, liveReducer, MAX_TIMELINE_EVENTS } from '../liveState'
import { buildActivity } from '../activity'

const PNG = 'iVBORw0KGgo='
let n = 0
const ev = (overrides = {}) => ({
  event_id: `e${++n}`,
  session_id: 's1',
  task_id: 't1',
  timestamp: '2026-11-23T10:00:00Z',
  event_type: 'action_started',
  description: 'Doing something',
  ...overrides,
})
const apply = (events, state = createLiveState()) => liveReducer(state, { type: 'events', events })

describe('normalizeEvent', () => {
  it('rejects events missing id, type or a valid timestamp', () => {
    expect(normalizeEvent({ event_type: 'navigation', timestamp: '2026-11-23T10:00:00Z' })).toBeNull()
    expect(normalizeEvent({ event_id: 'x', timestamp: '2026-11-23T10:00:00Z' })).toBeNull()
    expect(normalizeEvent({ event_id: 'x', event_type: 'navigation', timestamp: 'not a date' })).toBeNull()
  })

  it('only accepts http(s) urls and safe screenshot sources', () => {
    const e = normalizeEvent(ev({ url: 'javascript:alert(1)', screenshot: { url: 'javascript:alert(1)' } }))
    expect(e.url).toBeNull()
    expect(e.screenshot).toBeNull()
    const ok = normalizeEvent(ev({ url: 'https://example.com/a', screenshot: { data: PNG, mime_type: 'image/png', width: 1280, height: 800 } }))
    expect(ok.url).toBe('https://example.com/a')
    expect(ok.screenshot.src).toBe(`data:image/png;base64,${PNG}`)
    expect(normalizeEvent(ev({ screenshot: { url: '/api/browser/shot.png' } })).screenshot.src).toBe('/api/browser/shot.png')
  })

  it('masks typed text for sensitive fields and secrets in metadata', () => {
    const typed = normalizeEvent(ev({ event_type: 'keyboard_input', input: { field_label: 'Password', text_preview: 'hunter2' } }))
    expect(typed.input.text_preview).toBe(MASK)
    const flagged = normalizeEvent(ev({ event_type: 'keyboard_input', input: { field_label: 'Search', text_preview: 'x', masked: true } }))
    expect(flagged.input.text_preview).toBe(MASK)
    const meta = normalizeEvent(ev({ metadata: { api_key: 'abc', nested: { card_number: '4111' }, ok: 1 } }))
    expect(meta.metadata).toEqual({ api_key: MASK, nested: { card_number: MASK }, ok: 1 })
  })
})

describe('liveReducer', () => {
  it('orders the timeline by sequence, then timestamp, even when events arrive out of order', () => {
    const state = apply([
      ev({ event_id: 'c', sequence: 3, timestamp: '2026-11-23T10:00:03Z' }),
      ev({ event_id: 'a', sequence: 1, timestamp: '2026-11-23T10:00:01Z' }),
    ])
    const later = apply([ev({ event_id: 'b', sequence: 2, timestamp: '2026-11-23T10:00:02Z' })], state)
    expect(later.events.map((e) => e.event_id)).toEqual(['a', 'b', 'c'])
  })

  it('ignores duplicate events, e.g. replayed after a reconnect', () => {
    const first = apply([ev({ event_id: 'dup' }), ev({ event_id: 'dup' })])
    expect(first.events).toHaveLength(1)
    const again = apply([ev({ event_id: 'dup' })], first)
    expect(again).toBe(first)
  })

  it('keeps the newest screenshot and ignores stale (older) frames', () => {
    const newer = apply([ev({ event_type: 'screenshot_updated', screenshot: { data: PNG, captured_at: '2026-11-23T10:00:05Z', width: 10, height: 10 } })])
    const stale = apply([ev({ event_type: 'screenshot_updated', screenshot: { data: 'AAAA', captured_at: '2026-11-23T10:00:01Z' } })], newer)
    expect(stale.screenshot.src).toContain(PNG)
  })

  it('tracks cursor, click and typing only from real events and never moves backwards in time', () => {
    let s = apply([ev({ event_type: 'mouse_click', timestamp: '2026-11-23T10:00:05Z', cursor: { x: 50, y: 60 } })])
    expect(s.cursor).toMatchObject({ x: 50, y: 60 })
    expect(s.lastClick).toMatchObject({ x: 50, y: 60 })
    s = apply([ev({ event_type: 'mouse_move', timestamp: '2026-11-23T10:00:01Z', cursor: { x: 1, y: 1 } })], s)
    expect(s.cursor).toMatchObject({ x: 50, y: 60 })
    s = apply([ev({ event_type: 'keyboard_input', timestamp: '2026-11-23T10:00:06Z', input: { field_label: 'Search', text_preview: 'flights' } })], s)
    expect(s.typing).toMatchObject({ field_label: 'Search', text_preview: 'flights' })
  })

  it('tracks pause/resume from events and caps timeline size', () => {
    let s = apply([ev({ event_type: 'execution_paused', timestamp: '2026-11-23T10:00:01Z' })])
    expect(s.execution.state).toBe('paused')
    s = apply([ev({ event_type: 'execution_resumed', timestamp: '2026-11-23T10:00:02Z' })], s)
    expect(s.execution.state).toBe('running')
    const many = Array.from({ length: MAX_TIMELINE_EVENTS + 20 }, (_, i) => ev({ event_id: `m${i}`, sequence: i }))
    expect(apply(many).events).toHaveLength(MAX_TIMELINE_EVENTS)
  })
})

describe('deriveDisplayStatus', () => {
  it('only reports completion or cancellation when the backend task says so', () => {
    expect(deriveDisplayStatus({ taskStatus: 'executing', execution: 'ended' })).toBe('running')
    expect(deriveDisplayStatus({ taskStatus: 'executing', execution: 'cancelled' })).toBe('running')
    expect(deriveDisplayStatus({ taskStatus: 'completed' })).toBe('completed')
    expect(deriveDisplayStatus({ taskStatus: 'cancelled' })).toBe('cancelled')
  })

  it('maps pause, approvals, cancelling and initial states', () => {
    expect(deriveDisplayStatus({ taskStatus: 'executing', execution: 'paused' })).toBe('paused')
    expect(deriveDisplayStatus({ taskStatus: 'awaiting_approval' })).toBe('waiting_for_approval')
    expect(deriveDisplayStatus({ taskStatus: 'executing', cancelling: true })).toBe('cancelling')
    expect(deriveDisplayStatus({ taskStatus: 'created' })).toBe('initializing')
    expect(deriveDisplayStatus({ taskStatus: 'needs_attention' })).toBe('needs_attention')
    expect(deriveDisplayStatus({ taskStatus: 'failed' })).toBe('failed')
  })
})

describe('buildActivity', () => {
  it('merges browser and task events chronologically without duplicates', () => {
    const live = apply([ev({ event_id: 'b1', timestamp: '2026-11-23T10:00:02Z', description: 'Browser thing' })]).events
    const task = [
      { event_id: 't1', timestamp: '2026-11-23T10:00:01Z', event_type: 'task.created', message: 'Created', status: 'created' },
      { event_id: 't1', timestamp: '2026-11-23T10:00:01Z', event_type: 'task.created', message: 'Created', status: 'created' },
      { event_id: 't2', timestamp: '2026-11-23T10:00:03Z', event_type: 'task.completed', message: 'Done', status: 'completed' },
    ]
    expect(buildActivity(live, task).map((i) => i.description)).toEqual(['Created', 'Browser thing', 'Done'])
  })
})