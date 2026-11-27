// Transport tests use fake WebSocket / fetch implementations (mocked). Real streaming is untested
// until Member 2's browser backend exists.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createBrowserSessionService, getLiveConfig } from '../browserSessionService'
import { buildUrl, ConnectionStatus, MAX_RETRIES, openLiveConnection } from '../transport'

class FakeSocket {
  static instances = []
  constructor(url) {
    this.url = url
    this.closed = false
    FakeSocket.instances.push(this)
  }
  close() {
    this.closed = true
  }
  open() {
    this.onopen?.()
  }
  send(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
  drop() {
    this.onclose?.()
  }
}

const event = (id, sequence, type = 'navigation') => ({ event_id: id, sequence, event_type: type, timestamp: '2026-11-23T10:00:00Z' })

beforeEach(() => {
  FakeSocket.instances = []
  vi.useFakeTimers()
})
afterEach(() => vi.useRealTimers())

function connect(extra = {}) {
  const statuses = []
  const received = []
  const conn = openLiveConnection({
    transport: 'websocket',
    url: '/api/browser/sessions/{task_id}/events?after={after}',
    taskId: 'task-1',
    onEvents: (evs) => received.push(...evs),
    onStatus: (s) => statuses.push(s),
    deps: { WebSocket: FakeSocket },
    ...extra,
  })
  return { conn, statuses, received }
}

describe('openLiveConnection (websocket)', () => {
  it('connects, delivers events and reports status', () => {
    const { statuses, received } = connect()
    const socket = FakeSocket.instances[0]
    expect(socket.url).toBe('/api/browser/sessions/task-1/events?after=0')
    socket.open()
    socket.send([event('a', 1), event('b', 2)])
    socket.send({ events: [event('c', 3)] })
    socket.send(event('d', 4))
    expect(received.map((e) => e.event_id)).toEqual(['a', 'b', 'c', 'd'])
    expect(statuses).toEqual([ConnectionStatus.CONNECTING, ConnectionStatus.CONNECTED])
  })

  it('reconnects with backoff after a drop and asks only for newer events', () => {
    const { statuses } = connect()
    const first = FakeSocket.instances[0]
    first.open()
    first.send([event('a', 7)])
    first.drop()
    expect(statuses.at(-1)).toBe(ConnectionStatus.RECONNECTING)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
    expect(FakeSocket.instances[1].url).toContain('after=7')
    FakeSocket.instances[1].open()
    expect(statuses.at(-1)).toBe(ConnectionStatus.CONNECTED)
  })

  it('gives up after the retry limit and can be retried manually', () => {
    const { conn, statuses } = connect()
    for (let i = 0; i <= MAX_RETRIES; i++) {
      FakeSocket.instances.at(-1).drop()
      vi.advanceTimersByTime(60000)
    }
    expect(statuses.at(-1)).toBe(ConnectionStatus.DISCONNECTED)
    const count = FakeSocket.instances.length
    conn.retry()
    expect(FakeSocket.instances).toHaveLength(count + 1)
    expect(statuses.at(-1)).toBe(ConnectionStatus.CONNECTING)
  })

  it('cleans up on close: socket closed, no reconnect, no more callbacks', () => {
    const { conn, received } = connect()
    const socket = FakeSocket.instances[0]
    socket.open()
    conn.close()
    expect(socket.closed).toBe(true)
    socket.send([event('late', 1)])
    socket.drop()
    vi.advanceTimersByTime(60000)
    expect(received).toHaveLength(0)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('stops when the session ends', () => {
    const { statuses } = connect()
    const socket = FakeSocket.instances[0]
    socket.open()
    socket.send([event('end', 9, 'session_ended')])
    expect(statuses.at(-1)).toBe(ConnectionStatus.ENDED)
    expect(socket.closed).toBe(true)
  })
})

describe('openLiveConnection (polling)', () => {
  it('polls no faster than the minimum interval and passes the last sequence', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ events: [event('p1', 5)] }) })
    const statuses = []
    const conn = openLiveConnection({ transport: 'polling', url: '/x/{task_id}?after={after}', taskId: 't', pollMs: 10, onEvents: () => {}, onStatus: (s) => statuses.push(s), deps: { fetch } })
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('/x/t?after=0')
    await vi.advanceTimersByTimeAsync(500)
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(600)
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(fetch.mock.calls[1][0]).toBe('/x/t?after=5')
    expect(statuses).toContain(ConnectionStatus.CONNECTED)
    conn.close()
  })

  it('treats cancellation as terminal and stops polling', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ events: [event('cancelled', 1, 'cancelled')] }) })
    const statuses = []
    const conn = openLiveConnection({ transport: 'polling', url: '/x/{task_id}?after={after}', taskId: 't', onEvents: () => {}, onStatus: (s) => statuses.push(s), deps: { fetch } })
    await vi.advanceTimersByTimeAsync(0)
    expect(statuses.at(-1)).toBe(ConnectionStatus.ENDED)
    expect(fetch).toHaveBeenCalledTimes(1)
    conn.close()
  })
  it('reports reconnecting when a poll fails', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 })
    const statuses = []
    const conn = openLiveConnection({ transport: 'polling', url: '/x', taskId: 't', onEvents: () => {}, onStatus: (s) => statuses.push(s), deps: { fetch } })
    await vi.advanceTimersByTimeAsync(0)
    expect(statuses.at(-1)).toBe(ConnectionStatus.RECONNECTING)
    conn.close()
  })
})

describe('browserSessionService', () => {
  it('uses the task-scoped backend polling endpoint by default', () => {
    const config = getLiveConfig({})
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ events: [] }) })
    const service = createBrowserSessionService(config, { fetch })
    expect(config).toMatchObject({ transport: 'polling', eventsUrl: '/api/tasks/{task_id}/browser-events?after={after}' })
    expect(service.capabilities).toEqual({ liveEvents: true, pause: false, resume: false })
    const statuses = []
    const connection = service.connect('t', { onEvents: () => {}, onStatus: (s) => statuses.push(s) })
    expect(statuses).toEqual([ConnectionStatus.CONNECTING])
    connection.close()
  })

  it('allows live polling to be explicitly disabled', () => {
    const service = createBrowserSessionService(getLiveConfig({ VITE_LIVE_TRANSPORT: 'none' }))
    expect(service.capabilities.liveEvents).toBe(false)
  })

  it('rejects pause/resume instead of pretending they worked', async () => {
    const service = createBrowserSessionService(getLiveConfig({}))
    await expect(service.pause('t')).rejects.toThrow(/not supported/)
    await expect(service.resume('t')).rejects.toThrow(/not supported/)
  })

  it('calls the configured pause endpoint and surfaces backend errors', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, text: async () => '{"execution_state":"paused"}' })
      .mockResolvedValueOnce({ ok: false, status: 409, text: async () => '{"detail":{"code":"x","message":"Cannot pause now"}}' })
    const service = createBrowserSessionService(getLiveConfig({ VITE_LIVE_PAUSE_URL: '/api/browser/sessions/{task_id}/pause' }), { fetch })
    await expect(service.pause('abc')).resolves.toEqual({ execution_state: 'paused' })
    expect(fetch.mock.calls[0][0]).toBe('/api/browser/sessions/abc/pause')
    await expect(service.pause('abc')).rejects.toThrow('Cannot pause now')
  })

  it('builds urls with encoded placeholders', () => {
    expect(buildUrl('/a/{task_id}?after={after}', { task_id: 'x y', after: 3 })).toBe('/a/x%20y?after=3')
  })
})
