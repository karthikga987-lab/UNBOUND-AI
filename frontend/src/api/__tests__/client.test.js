// Checks the client against the request/response shapes of Member 3's real task API
// (feature/orchestration-engine). fetch is mocked here; the shapes were also checked manually
// against the running FastAPI app.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, realApi } from '../client'

afterEach(() => vi.unstubAllGlobals())

const respond = (status, body) => vi.fn().mockResolvedValue({ ok: status < 400, status, text: async () => JSON.stringify(body) })

describe('realApi', () => {
  it('loads bounded persisted task history', async () => {
    const fetch = respond(200, [])
    vi.stubGlobal('fetch', fetch)
    await realApi.listTasks(25)
    expect(fetch.mock.calls[0][0]).toBe('/api/tasks?limit=25')
  })

  it('uses explicit confirmation for private S3 sync and safe restore routes', async () => {
    const fetch = respond(200, { enabled: true, status: 'synchronized' })
    vi.stubGlobal('fetch', fetch)
    await realApi.s3SyncStatus()
    expect(fetch.mock.calls[0][0]).toBe('/api/storage/s3/status')
    await realApi.syncToS3()
    expect(fetch.mock.calls[1][0]).toBe('/api/storage/s3/sync')
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ confirm_sync: true })
    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => '[]' })
    await realApi.listS3Backups()
    expect(fetch.mock.calls[2][0]).toBe('/api/storage/s3/backups')
    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => JSON.stringify({ verified: true }) })
    await realApi.restoreTaskBackup('unbound/backups/synthetic.zip')
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toEqual({
      object_key: 'unbound/backups/synthetic.zip', confirm_restore: true,
    })
  })

  it('loads task references scoped to a conversation session', async () => {
    const fetch = respond(200, { session_id: 'session-1', available_task_ids: ['task-1'], unavailable_task_ids: [] })
    vi.stubGlobal('fetch', fetch)
    const result = await realApi.getConversationTaskReferences('session-1')
    expect(fetch.mock.calls[0][0]).toBe('/api/conversations/session-1/tasks')
    expect(result.available_task_ids).toEqual(['task-1'])
  })

  it('uses backend classification, conversation sessions, and assistant task preparation endpoints', async () => {
    const fetch = respond(201, { session_id: 'session-1' })
    vi.stubGlobal('fetch', fetch)
    await realApi.createConversation()
    expect(fetch.mock.calls[0][0]).toBe('/api/conversations')
    expect(fetch.mock.calls[0][1].method).toBe('POST')

    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => JSON.stringify({ session_id: 'session-1', messages: [] }) })
    await realApi.getConversation('session-1')
    expect(fetch.mock.calls[1][0]).toBe('/api/conversations/session-1')

    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => JSON.stringify({ category: 'information' }) })
    await realApi.classifyAssistantRequest('How does verification work?', 'session-1')
    expect(fetch.mock.calls[2][0]).toBe('/api/assistant/classify')
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ message: 'How does verification work?', session_id: 'session-1' })

    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => JSON.stringify({ session_id: 'session-1', reply: 'Answer', user_message: {}, assistant_message: {} }) })
    await realApi.sendConversationMessage('session-1', 'How does verification work?', 'Asia/Kolkata')
    expect(fetch.mock.calls[3][0]).toBe('/api/conversations/session-1/messages')
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toMatchObject({ message: 'How does verification work?', timezone: 'Asia/Kolkata' })
    expect(JSON.parse(fetch.mock.calls[3][1].body).request_id).toMatch(/^[0-9a-f-]{36}$/i)

    fetch.mockResolvedValueOnce({ ok: true, status: 200, text: async () => JSON.stringify({ intent: { category: 'action_execution' }, reply: 'Task prepared', task: null }) })
    await realApi.assistantRequest('How does verification work?', 'session-1')
    const [url, init] = fetch.mock.calls[4]
    expect(url).toBe('/api/assistant/requests')
    expect(JSON.parse(init.body)).toEqual({ message: 'How does verification work?', session_id: 'session-1' })
  })

  it('sends the approval decision in the uppercase form the backend requires', async () => {
    const fetch = respond(200, { task_id: 't' })
    vi.stubGlobal('fetch', fetch)
    await realApi.decideApproval('t', 'a', 'approved')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/tasks/t/approvals/a')
    expect(JSON.parse(init.body)).toEqual({ decision: 'APPROVED' })
    await realApi.decideApproval('t', 'a', 'rejected')
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ decision: 'REJECTED' })
  })

  it('surfaces the backend error message and code from {detail: {code, message}}', async () => {
    vi.stubGlobal('fetch', respond(503, { detail: { code: 'llm_unavailable', message: 'LLM provider is not configured' } }))
    const error = await realApi.createTask('find flights').catch((e) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.message).toBe('LLM provider is not configured')
    expect(error.code).toBe('llm_unavailable')
    expect(error.status).toBe(503)
  })

  it('reports a clear message when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(realApi.health()).rejects.toThrow(/Cannot reach the backend/)
  })
})
