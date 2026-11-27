import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api'
import { canExecute } from '../../api/contracts'
import { useTask } from '../../hooks/useTask'
import TaskWorkspace from '../TaskWorkspace'

vi.mock('../../api', () => ({
  api: {
    decideApproval: vi.fn(),
    executeTask: vi.fn(),
    cancelTask: vi.fn(),
  },
}))

vi.mock('../../hooks/useTask', () => ({ useTask: vi.fn() }))
vi.mock('../live/LiveView', () => ({ default: ({ task, taskEvents }) => (
  <div data-testid="live-view-status">{task.status} {taskEvents.map((event) => event.message).join(' ')}</div>
) }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TaskWorkspace approval continuation', () => {
  it('records approval but waits for an explicit Run plan click', async () => {
    const taskId = '2f05c1d2-1d13-4f1b-ae1e-8d5c2ff0da11'
    const approvalId = 'b8b79fb0-676b-4b21-84ef-7bdd86c99791'
    const approval = {
      approval_id: approvalId,
      task_id: taskId,
      step_id: 'send_message',
      tool_name: 'browser.click',
      description: 'Send the message',
      arguments: { target: { role: 'button', name: 'Send' } },
      status: 'pending',
      created_at: '2026-11-24T10:00:00Z',
    }
    const task = {
      task_id: taskId,
      user_request: 'Send the requested message',
      status: 'awaiting_approval',
      plan: [{
        step_id: 'send_message',
        tool_name: 'browser.click',
        description: 'Send the message',
        input: approval.arguments,
        expected_outcome: 'Message is sent',
        requires_approval: true,
        status: 'awaiting_approval',
        verification: null,
        error: null,
        depends_on: [],
      }],
      current_step: 'send_message',
      completed_steps: [],
      pending_approvals: [approval],
      evidence: [],
      artifacts: [],
      result: null,
      error: null,
    }
    const updated = { ...task, status: 'created', pending_approvals: [], plan: [{ ...task.plan[0], status: 'pending' }] }
    useTask.mockReturnValue({ task, events: [], error: null, refresh: vi.fn().mockResolvedValue(updated), setTask: vi.fn() })
    api.decideApproval.mockResolvedValue(updated)

    render(<TaskWorkspace taskId={taskId} onTaskChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledWith(taskId, approvalId, 'approved'))
    expect(api.executeTask).not.toHaveBeenCalled()
  })

  it('does not offer generic execution for needs-attention tasks', () => {
    expect(canExecute('needs_attention')).toBe(false)
    expect(canExecute('created')).toBe(true)
  })

  it('shows balanced Results and scrollable chronological Activity panels below the plan', () => {
    const taskId = 'd438eaa3-e838-442c-9f81-efc74ab2f4fa'
    const task = {
      task_id: taskId,
      user_request: 'Summarize a verified page',
      status: 'completed',
      plan: [],
      pending_approvals: [],
      completed_steps: [],
      evidence: [],
      artifacts: [],
      result: { summary: 'A short verified result.' },
    }
    const events = [
      { event_id: 'activity-1', timestamp: '2026-11-27T10:00:00Z', event_type: 'task_started', message: 'Task started', status: 'running' },
      { event_id: 'activity-2', timestamp: '2026-11-27T10:00:02Z', event_type: 'task_completed', message: 'Task completed', status: 'completed' },
    ]
    useTask.mockReturnValue({ task, events, error: null, refresh: vi.fn(), setTask: vi.fn() })

    render(<TaskWorkspace taskId={taskId} onTaskChange={vi.fn()} />)

    const grid = screen.getByTestId('results-activity-grid')
    expect(grid.className).toContain('lg:grid-cols-2')
    expect(screen.getByRole('heading', { name: 'Results' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Activity' })).toBeTruthy()
    const activityRegion = screen.getByRole('region', { name: 'Activity events' })
    expect(activityRegion.tabIndex).toBe(0)
    expect(activityRegion.className).toContain('max-h-80')
    const items = screen.getAllByTestId('activity-item')
    expect(items).toHaveLength(2)
    expect(items[0].textContent).toContain('Task started')
    expect(items[1].textContent).toContain('Task completed')
    expect(screen.getByRole('button', { name: 'View full result' })).toBeTruthy()
  })

  it('runs the selected saved plan once and opens live task status', async () => {
    const taskId = '480312ac-4245-4abf-a1bd-1f06da43aac9'
    const initial = {
      task_id: taskId,
      user_request: 'Open the approved page and summarize it',
      status: 'created',
      plan: [],
      pending_approvals: [],
      completed_steps: [],
      evidence: [],
      artifacts: [],
    }
    const running = { ...initial, status: 'running' }
    let finish
    api.executeTask.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    useTask.mockReturnValue({
      task: initial,
      events: [{ event_id: 'event-1', message: 'Browser navigation started', status: 'running' }],
      error: null,
      refresh: vi.fn().mockResolvedValue(running),
      setTask: vi.fn((updated) => Object.assign(initial, updated)),
    })
    render(<TaskWorkspace taskId={taskId} onTaskChange={vi.fn()} />)

    const runButton = screen.getByRole('button', { name: 'Run plan' })
    act(() => {
      runButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      runButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })

    expect(api.executeTask).toHaveBeenCalledTimes(1)
    expect(api.executeTask).toHaveBeenCalledWith(taskId)
    expect(screen.getByRole('tab', { name: 'Live View' }).getAttribute('aria-selected')).toBe('true')
    await act(async () => { finish(running) })
    await waitFor(() => expect(screen.getByTestId('live-view-status').textContent).toContain('running'))
    expect(screen.getByTestId('live-view-status').textContent).toContain('Browser navigation started')
  })
})
