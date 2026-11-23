// Checks the client against the request/response shapes of Member 3's real task API
// (feature/orchestration-engine). fetch is mocked here; the shapes were also checked manually
// against the running FastAPI app.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, realApi } from '../client'

afterEach(() => vi.unstubAllGlobals())

const respond = (status, body) => vi.fn().mockResolvedValue({ ok: status < 400, status, text: async () => JSON.stringify(body) })

describe('realApi', () => {
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