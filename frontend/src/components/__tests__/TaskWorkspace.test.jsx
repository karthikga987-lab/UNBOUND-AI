import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
vi.mock('../live/LiveView', () => ({ default: () => null }))

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
})
