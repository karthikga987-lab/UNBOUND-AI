// Component tests with a fake browser session service (mocked). They verify the UI reacts correctly
// to contract-shaped events; they do NOT prove that a real browser backend streams correctly.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LiveView from '../LiveView'

const PNG = 'iVBORw0KGgo='
let seq = 0

function fakeService({ liveEvents = true, pause = false, resume = false, pauseResult, resumeResult } = {}) {
  const handlers = {}
  const retry = vi.fn()
  return {
    capabilities: { liveEvents, pause, resume },
    connect: vi.fn((taskId, h) => {
      Object.assign(handlers, h)
      h.onStatus(liveEvents ? 'connecting' : 'unavailable')
      return { close: vi.fn(), retry }
    }),
    pause: vi.fn(async () => pauseResult),
    resume: vi.fn(async () => resumeResult),
    retry,
    push: (events) => act(() => handlers.onEvents(events)),
    setStatus: (s) => act(() => handlers.onStatus(s)),
  }
}

const liveEvent = (type, extra = {}) => ({
  event_id: `ev-${++seq}`,
  session_id: 's1',
  sequence: seq,
  timestamp: new Date(Date.UTC(2026, 10, 23, 10, 0, seq)).toISOString(),
  event_type: type,
  ...extra,
})
const screenshot = (extra = {}) => liveEvent('screenshot_updated', { screenshot: { data: PNG, width: 1000, height: 500, captured_at: new Date().toISOString() }, ...extra })

function makeTask(overrides = {}) {
  return {
    task_id: '11111111-1111-4111-8111-111111111111',
    user_request: 'Find flights to Goa',
    status: 'executing',
    plan: [
      { step_id: 'step_1', tool_name: 'browser.navigate', description: 'Open airline site', input: {}, status: 'completed', requires_approval: false, expected_outcome: 'Loaded', verification: { verified: true }, error: null },
      { step_id: 'step_2', tool_name: 'browser.fill', description: 'Search flights', input: {}, status: 'running', requires_approval: false, expected_outcome: 'Results shown', verification: null, error: null },
    ],
    current_step: 'step_2',
    completed_steps: ['step_1'],
    pending_approvals: [],
    evidence: [],
    artifacts: [],
    result: null,
    error: null,
    created_at: '2026-11-23T10:00:00Z',
    updated_at: '2026-11-23T10:00:00Z',
    ...overrides,
  }
}

function renderLive({ service = fakeService(), task = makeTask(), ...props } = {}) {
  const handlers = { onCancel: vi.fn(), onDecide: vi.fn(), onBack: vi.fn() }
  const utils = render(<LiveView task={task} taskEvents={[]} busy={null} actionError={null} service={service} {...handlers} {...props} />)
  const rerender = (next) => utils.rerender(<LiveView task={task} taskEvents={[]} busy={null} actionError={null} service={service} {...handlers} {...props} {...next} />)
  return { service, handlers, rerender }
}

afterEach(() => cleanup())

describe('LiveView', () => {
  it('shows an explicit unavailable state and disabled controls when the backend has no live capability', () => {
    renderLive({ service: fakeService({ liveEvents: false }) })
    expect(screen.getByTestId('live-placeholder-unavailable')).toBeTruthy()
    expect(screen.queryByTestId('live-screenshot')).toBeNull()
    expect(screen.getByRole('button', { name: /pause/i }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: /resume/i }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: /stop/i }).disabled).toBe(false)
  })

  it('shows loading before the first screenshot, then renders and updates the live screen', () => {
    const { service } = renderLive()
    expect(screen.getByTestId('live-placeholder-connecting')).toBeTruthy()
    service.setStatus('connected')
    expect(screen.getByTestId('live-placeholder-connected')).toBeTruthy()
    service.push([liveEvent('session_started', { description: 'Browser session started' }), screenshot()])
    const img = screen.getByTestId('live-screenshot')
    expect(img.getAttribute('src')).toBe(`data:image/png;base64,${PNG}`)
    service.push([screenshot({ screenshot: { data: 'QUJDRA==', width: 1000, height: 500, captured_at: new Date(Date.now() + 1000).toISOString() } })])
    expect(screen.getByTestId('live-screenshot').getAttribute('src')).toBe('data:image/png;base64,QUJDRA==')
  })

  it('draws cursor, click and target overlays at the reported coordinates', () => {
    const { service } = renderLive()
    service.setStatus('connected')
    service.push([screenshot(), liveEvent('mouse_click', { cursor: { x: 500, y: 125 }, target: { label: 'Search', bbox: { x: 100, y: 50, width: 200, height: 50 } } })])
    expect(screen.getByTestId('cursor').style.left).toBe('50%')
    expect(screen.getByTestId('cursor').style.top).toBe('25%')
    expect(screen.getByTestId('click-indicator').style.left).toBe('50%')
    expect(screen.getByTestId('target-box').style.width).toBe('20%')
    service.push([liveEvent('keyboard_input', { input: { field_label: 'From', text_preview: 'Mumbai' } })])
    expect(screen.getByTestId('typing-indicator').textContent).toContain('Mumbai')
  })

  it('never invents overlays when no events have been received', () => {
    const { service } = renderLive()
    service.setStatus('connected')
    service.push([screenshot()])
    expect(screen.queryByTestId('cursor')).toBeNull()
    expect(screen.queryByTestId('click-indicator')).toBeNull()
    expect(screen.queryByTestId('typing-indicator')).toBeNull()
  })

  it('lists activity in chronological order without duplicates', () => {
    const { service } = renderLive()
    const late = liveEvent('page_loaded', { description: 'Results page loaded' })
    const early = liveEvent('navigation', { description: 'Opened airline site', sequence: 0, timestamp: '2026-11-23T09:59:00Z' })
    service.push([late, early, late])
    const items = screen.getAllByTestId('activity-item').map((li) => li.textContent)
    expect(items).toHaveLength(2)
    expect(items[0]).toContain('Opened airline site')
    expect(items[1]).toContain('Results page loaded')
  })

  it('handles connection loss, keeps the last frame and allows a manual reconnect', () => {
    const { service } = renderLive()
    service.setStatus('connected')
    service.push([screenshot()])
    service.setStatus('reconnecting')
    expect(screen.getByTestId('offline-banner').textContent).toMatch(/reconnecting/i)
    expect(screen.getByTestId('live-warnings').textContent).toMatch(/reconnecting/i)
    service.setStatus('disconnected')
    fireEvent.click(within(screen.getByTestId('offline-banner')).getByRole('button', { name: /reconnect/i }))
    expect(service.retry).toHaveBeenCalled()
    service.setStatus('connected')
    expect(screen.queryByTestId('offline-banner')).toBeNull()
  })

  it('displays approval details and forwards the decision to the backend handler', () => {
    const approval = {
      approval_id: '22222222-2222-4222-8222-222222222222',
      task_id: '11111111-1111-4111-8111-111111111111',
      step_id: 'step_3',
      tool_name: 'browser.click',
      description: 'Confirm flight booking',
      arguments: { url: 'https://airline.example/checkout', amount: 5400, currency: 'INR', form_data: { name: 'Asha', card_number: '4111111111111111' } },
      status: 'pending',
      created_at: '2026-11-23T10:00:00Z',
    }
    const { handlers } = renderLive({ task: makeTask({ status: 'awaiting_approval', pending_approvals: [approval] }) })
    const card = within(screen.getByTestId('live-approvals'))
    expect(card.getByTestId('approval-amount').textContent).toBe('5400 INR')
    expect(card.getByText('https://airline.example/checkout')).toBeTruthy()
    expect(screen.getByTestId('live-approvals').textContent).not.toContain('4111111111111111')
    expect(screen.getAllByText('Waiting for approval').length).toBeGreaterThan(0)
    fireEvent.click(card.getByRole('button', { name: 'Approve' }))
    expect(handlers.onDecide).toHaveBeenCalledWith(approval, 'approved')
  })

  it('reflects pause and resume only once the backend confirms', async () => {
    const service = fakeService({ pause: true, resume: true, pauseResult: { execution_state: 'paused' }, resumeResult: {} })
    renderLive({ service })
    const status = () => within(screen.getByTestId('live-status'))
    expect(status().getAllByText('Running').length).toBeGreaterThan(0)

    await act(async () => fireEvent.click(screen.getByRole('button', { name: /pause/i })))
    expect(service.pause).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111')
    expect(status().getAllByText('Paused').length).toBeGreaterThan(0)

    // Resume response carries no state, so the UI waits for the backend event.
    await act(async () => fireEvent.click(screen.getByRole('button', { name: /resume/i })))
    expect(screen.getByTestId('live-warnings').textContent).toMatch(/resume requested/i)
    expect(status().getAllByText('Paused').length).toBeGreaterThan(0)
    service.push([liveEvent('execution_resumed', { timestamp: new Date(Date.now() + 5000).toISOString() })])
    expect(status().getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('shows a pause error from the backend instead of pretending it worked', async () => {
    const service = fakeService({ pause: true })
    service.pause.mockRejectedValueOnce(new Error('Cannot pause during navigation'))
    renderLive({ service })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: /pause/i })))
    expect(screen.getByTestId('live-warnings').textContent).toContain('Cannot pause during navigation')
    expect(within(screen.getByTestId('live-status')).getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('shows Cancelling while the stop request is pending and Cancelled only after the backend confirms', () => {
    const { handlers, rerender } = renderLive()
    fireEvent.click(screen.getByRole('button', { name: /stop/i }))
    expect(handlers.onCancel).toHaveBeenCalled()
    rerender({ busy: 'cancel' })
    expect(screen.getAllByText('Cancelling…').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: /stopping/i }).disabled).toBe(true)
    rerender({ busy: null, task: makeTask({ status: 'cancelled', current_step: null }) })
    expect(screen.getAllByText('Cancelled').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: /stop/i }).disabled).toBe(true)
  })

  it('renders an empty session and a failed session clearly', () => {
    const { service, rerender } = renderLive({ task: makeTask({ status: 'created', current_step: null }) })
    expect(screen.getByText('No activity yet')).toBeTruthy()
    expect(screen.getAllByText('Initializing').length).toBeGreaterThan(0)
    service.push([liveEvent('execution_failed', { description: 'Login page did not load' })])
    rerender({ task: makeTask({ status: 'failed', error: 'Browser step failed' }) })
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0)
    const warnings = screen.getByTestId('live-warnings').textContent
    expect(warnings).toContain('Browser step failed')
    expect(warnings).toContain('Login page did not load')
  })

  it('shows an ended state when the session ends without any screenshot', () => {
    const { service } = renderLive()
    service.setStatus('ended')
    expect(screen.getByTestId('live-placeholder-ended')).toBeTruthy()
  })
})