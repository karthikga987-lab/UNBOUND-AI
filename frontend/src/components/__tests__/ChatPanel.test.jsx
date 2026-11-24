import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({ onUtterance: null, executeTask: vi.fn() }))

vi.mock('../../api', () => ({
  api: { executeTask: state.executeTask },
}))

vi.mock('../../voice/useVoiceMode', () => ({
  useVoiceMode: ({ onUtterance }) => {
    state.onUtterance = onUtterance
    return {
      active: true,
      status: 'listening',
      transcript: '',
      message: '',
      audioRef: { current: { kind: null, micLevel: 0, boundaryAt: 0 } },
      start: vi.fn(),
      stop: vi.fn(),
      tap: vi.fn(),
      toggleMute: vi.fn(),
      announce: vi.fn(),
    }
  },
}))

import ChatPanel from '../ChatPanel'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  state.onUtterance = null
})

describe('voice task handoff', () => {
  it('does not execute a needs-attention task from the spoken run command', async () => {
    const task = {
      task_id: 'task-1',
      status: 'needs_attention',
      error: 'Outcome unknown',
      plan: [],
      completed_steps: [],
    }
    render(<ChatPanel history={[]} activeTaskId={task.task_id} activeTask={task} onSelect={vi.fn()} onSubmit={vi.fn()} />)

    let reply
    await act(async () => { reply = await state.onUtterance('run it') })

    expect(state.executeTask).not.toHaveBeenCalled()
    expect(reply).toMatch(/needs your attention/i)
    expect(reply).toMatch(/will not retry/i)
  })
})
