import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const state = vi.hoisted(() => ({
  api: {
    health: vi.fn(),
    createConversation: vi.fn(),
    getConversation: vi.fn(),
    getConversationTaskReferences: vi.fn(),
    classifyAssistantRequest: vi.fn(),
    sendConversationMessage: vi.fn(),
    assistantRequest: vi.fn(),
    createTask: vi.fn(),
    executeTask: vi.fn(),
    cancelTask: vi.fn(),
  },
  onUtterance: null,
}))

vi.mock('./api', () => ({ api: state.api, MOCK_AREAS: new Set(), USE_MOCK: false }))
vi.mock('./components/TaskWorkspace', () => ({ default: ({ taskId }) => <div data-testid="task-workspace">{taskId}</div> }))
vi.mock('./components/WorkflowPanel', () => ({ default: () => <div /> }))
vi.mock('./components/DataPanel', () => ({ default: () => <div /> }))
vi.mock('./voice/useVoiceMode', () => ({
  useVoiceMode: ({ onUtterance }) => {
    state.onUtterance = onUtterance
    return {
      active: false,
      status: 'idle',
      transcript: '',
      message: '',
      audioRef: { current: null },
      start: vi.fn(),
      stop: vi.fn(),
      tap: vi.fn(),
      toggleMute: vi.fn(),
      announce: vi.fn(),
    }
  },
}))

import App from './App'

const session = { session_id: 'session-1' }

beforeEach(() => {
  window.localStorage.clear()
  vi.clearAllMocks()
  state.api.health.mockResolvedValue({ status: 'ok' })
  state.api.createConversation.mockResolvedValue(session)
  state.api.getConversation.mockResolvedValue({ ...session, messages: [] })
  state.api.getConversationTaskReferences.mockResolvedValue({ session_id: session.session_id, available_task_ids: [], unavailable_task_ids: [] })
  state.api.classifyAssistantRequest.mockResolvedValue({ category: 'information' })
  state.api.sendConversationMessage.mockResolvedValue({
    session_id: session.session_id,
    reply: 'UNBOUND says verification requires an independent check.',
    user_message: { message_id: 'user-1', role: 'user', content: 'How does UNBOUND verify a task result?' },
    assistant_message: { message_id: 'assistant-1', role: 'assistant', content: 'UNBOUND says verification requires an independent check.' },
  })
  state.api.assistantRequest.mockResolvedValue({
    intent: { category: 'information' },
    reply: 'UNBOUND says verification requires an independent check.',
    task: null,
  })
})

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})

describe('React assistant chat routing', () => {
  it('restores a session and routes ordinary questions through the persistent conversation API without creating a task', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'How does UNBOUND verify a task result?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText(/independent check/i)).toBeTruthy()
    expect(state.api.getConversation).toHaveBeenCalledWith(session.session_id)
    expect(state.api.classifyAssistantRequest).toHaveBeenCalledWith('How does UNBOUND verify a task result?', session.session_id)
    expect(state.api.sendConversationMessage).toHaveBeenCalledWith(session.session_id, 'How does UNBOUND verify a task result?', expect.any(String))
    expect(state.api.assistantRequest).not.toHaveBeenCalled()
    expect(state.api.createTask).not.toHaveBeenCalled()
    expect(state.api.executeTask).not.toHaveBeenCalled()
  })

  it('surfaces a prepared action in the task workspace without automatically executing it', async () => {
    const task = {
      task_id: 'task-prepared-1',
      status: 'created',
      plan: [],
      pending_approvals: [],
      completed_steps: [],
      evidence: [],
      artifacts: [],
    }
    state.api.assistantRequest.mockResolvedValueOnce({
      intent: { category: 'action_execution', current_turn_authorizes_execution: true },
      reply: 'Task plan is ready. Nothing has been executed.',
      task,
    })
    state.api.classifyAssistantRequest.mockResolvedValueOnce({
      category: 'action_execution',
      current_turn_authorizes_execution: true,
    })
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'Prepare this action for review.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect((await screen.findByTestId('task-workspace')).textContent).toContain('task-prepared-1')
    expect(screen.getByText(/nothing has been executed/i)).toBeTruthy()
    expect(state.api.executeTask).not.toHaveBeenCalled()
    expect(state.api.createTask).not.toHaveBeenCalled()
  })

  it('keeps backend errors visible and leaves the unsent text in the composer', async () => {
    state.api.classifyAssistantRequest.mockResolvedValueOnce({ category: 'action_execution', current_turn_authorizes_execution: true })
    state.api.assistantRequest.mockRejectedValueOnce(new Error('The local model is unavailable'))
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'Explain the local model status.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect((await screen.findByRole('alert')).textContent).toContain('The local model is unavailable')
    await waitFor(() => expect(input.value).toBe('Explain the local model status.'))
  })

  it('restores the latest task associated with a conversation after a page reload', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    state.api.getConversationTaskReferences.mockResolvedValueOnce({
      session_id: session.session_id,
      available_task_ids: ['completed-document-task'],
      unavailable_task_ids: [],
    })
    render(<App />)

    expect((await screen.findByTestId('task-workspace')).textContent).toContain('completed-document-task')
    expect(state.api.getConversationTaskReferences).toHaveBeenCalledWith(session.session_id)
  })

  it('clearly reports linked tasks unavailable after a backend restart', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    state.api.getConversationTaskReferences.mockResolvedValueOnce({
      session_id: session.session_id,
      available_task_ids: [],
      unavailable_task_ids: ['lost-task'],
    })
    render(<App />)

    const status = await screen.findByRole('status')
    expect(status.textContent).toMatch(/task result is unavailable/i)
    expect(status.textContent).toContain('No file was reopened')
  })
})
