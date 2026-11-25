import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const state = vi.hoisted(() => ({
  api: {
    health: vi.fn(),
    createConversation: vi.fn(),
    getConversation: vi.fn(),
    getConversationTaskReferences: vi.fn(),
    getTask: vi.fn(),
    decideApproval: vi.fn(),
    classifyAssistantRequest: vi.fn(),
    sendConversationMessage: vi.fn(),
    assistantRequest: vi.fn(),
    createTask: vi.fn(),
    listTasks: vi.fn(),
    executeTask: vi.fn(),
    cancelTask: vi.fn(),
    s3SyncStatus: vi.fn(),
    listS3Backups: vi.fn(),
    syncToS3: vi.fn(),
    restoreTaskBackup: vi.fn(),
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
  state.api.getTask.mockResolvedValue({ task_id: 'task-1', status: 'created', pending_approvals: [], plan: [] })
  state.api.listTasks.mockResolvedValue([])
  state.api.s3SyncStatus.mockResolvedValue({ enabled: false, status: 'disabled', pending_count: 0, retry_count: 0 })
  state.api.listS3Backups.mockResolvedValue([])
  state.api.decideApproval.mockResolvedValue({ task_id: 'task-1', status: 'created', pending_approvals: [], plan: [] })
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
  it('loads persisted task history and opens a selected task', async () => {
    state.api.listTasks.mockResolvedValueOnce([{
      task_id: 'stored-task-1',
      user_request: 'Analyze the synthetic report',
      status: 'completed',
      updated_at: '2026-11-25T10:00:00Z',
    }])
    render(<App />)
    await screen.findByRole('button', { name: /Analyze the synthetic report/i })
    fireEvent.click(screen.getByRole('button', { name: /Analyze the synthetic report/i }))
    expect((await screen.findByTestId('task-workspace')).textContent).toContain('stored-task-1')
    expect(state.api.listTasks).toHaveBeenCalledWith(100)
  })

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

  it('binds a typed approve response to the linked pending approval without executing the task', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    state.api.getConversation.mockResolvedValueOnce({
      ...session,
      messages: [{ message_id: 'assistant-plan', role: 'assistant', content: 'File read task is ready for approval.', task_id: 'file-task' }],
    })
    state.api.getConversationTaskReferences.mockResolvedValueOnce({
      session_id: session.session_id,
      available_task_ids: ['file-task'],
      unavailable_task_ids: [],
    })
    const approval = { approval_id: 'approval-file', tool_name: 'document.read_local_file', step_id: 'read' }
    state.api.getTask.mockResolvedValueOnce({
      task_id: 'file-task', status: 'awaiting_approval', pending_approvals: [approval], plan: [],
    })
    state.api.decideApproval.mockResolvedValueOnce({
      task_id: 'file-task', status: 'created', pending_approvals: [], plan: [],
    })
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'approve' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText(/approval was recorded for the exact pending action/i)).toBeTruthy()
    expect(state.api.getTask).toHaveBeenCalledWith('file-task')
    expect(state.api.decideApproval).toHaveBeenCalledWith('file-task', 'approval-file', 'approved')
    expect(state.api.classifyAssistantRequest).not.toHaveBeenCalled()
    expect(state.api.assistantRequest).not.toHaveBeenCalled()
    expect(state.api.executeTask).not.toHaveBeenCalled()
  })

  it('keeps a file approval bound when a browser task is submitted next', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    state.api.getConversation.mockResolvedValueOnce({
      ...session,
      messages: [{ message_id: 'assistant-file-plan', role: 'assistant', content: 'File read task is ready for approval.', task_id: 'file-task' }],
    })
    state.api.getConversationTaskReferences.mockResolvedValueOnce({
      session_id: session.session_id,
      available_task_ids: ['file-task'],
      unavailable_task_ids: [],
    })
    state.api.getTask.mockResolvedValueOnce({
      task_id: 'file-task',
      status: 'awaiting_approval',
      pending_approvals: [{ approval_id: 'file-approval', tool_name: 'document.read_local_file' }],
      plan: [],
    })
    state.api.decideApproval.mockResolvedValueOnce({
      task_id: 'file-task', status: 'created', pending_approvals: [], plan: [],
    })
    state.api.classifyAssistantRequest.mockResolvedValueOnce({ category: 'action_execution' })
    state.api.assistantRequest.mockResolvedValueOnce({
      intent: { category: 'action_execution' },
      reply: 'Browser task is prepared.',
      task: { task_id: 'browser-task', status: 'created', pending_approvals: [], plan: [{ tool_name: 'browser.navigate', input: { url: 'http://unbound-demo.invalid/product' } }] },
    })
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'approve' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText(/approval was recorded for the exact pending action/i)).toBeTruthy()

    fireEvent.change(input, { target: { value: 'Open http://unbound-demo.invalid/product and summarize it.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText('Browser task is prepared.')).toBeTruthy()
    expect(state.api.decideApproval).toHaveBeenCalledTimes(1)
    expect(state.api.decideApproval).toHaveBeenCalledWith('file-task', 'file-approval', 'approved')
    expect(state.api.assistantRequest).toHaveBeenCalledWith('Open http://unbound-demo.invalid/product and summarize it.', session.session_id)
    expect(state.api.executeTask).not.toHaveBeenCalled()
  })

  it('does not treat approve as authorization when the latest chat message has no linked task', async () => {
    window.localStorage.setItem('unbound.conversation_id', session.session_id)
    state.api.getConversation.mockResolvedValueOnce({
      ...session,
      messages: [{ message_id: 'assistant-clarification', role: 'assistant', content: 'Please provide a valid path.' }],
    })
    state.api.getConversationTaskReferences.mockResolvedValueOnce({
      session_id: session.session_id,
      available_task_ids: ['unrelated-old-task'],
      unavailable_task_ids: [],
    })
    render(<App />)

    const input = await screen.findByPlaceholderText(/ask a question/i)
    fireEvent.change(input, { target: { value: 'approve' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(await screen.findByText(/could not match that response to a task/i)).toBeTruthy()
    expect(state.api.getTask).not.toHaveBeenCalled()
    expect(state.api.decideApproval).not.toHaveBeenCalled()
    expect(state.api.classifyAssistantRequest).not.toHaveBeenCalled()
    expect(state.api.assistantRequest).not.toHaveBeenCalled()
    expect(state.api.executeTask).not.toHaveBeenCalled()
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
