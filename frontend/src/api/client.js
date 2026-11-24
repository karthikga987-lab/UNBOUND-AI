// Real backend client. Endpoints follow docs/API_CONTRACT.md.
// Default '' = same origin: the Vite dev server proxies /api to the backend (see vite.config.js),
// which avoids CORS. Set VITE_API_BASE_URL only if the backend is served elsewhere with CORS enabled.
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message, { status, code } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

// Backend errors are {"detail": {"code": "...", "message": "..."}}; FastAPI defaults may be a string or list.
function formatDetail(detail) {
  if (!detail) return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d?.msg ?? d?.message ?? JSON.stringify(d)).join('; ')
  if (typeof detail.message === 'string') return detail.message
  return JSON.stringify(detail)
}

async function request(path, { method = 'GET', body, formData } = {}) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: formData ?? (body !== undefined ? JSON.stringify(body) : undefined),
    })
  } catch {
    throw new ApiError(`Cannot reach the backend at ${API_BASE_URL || 'the /api proxy'}. Is it running?`, { status: 0, code: 'network_error' })
  }
  const text = await response.text()
  let data = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }
  if (!response.ok) {
    throw new ApiError(formatDetail(data?.detail) ?? `Request failed (${response.status})`, {
      status: response.status,
      code: data?.detail?.code ?? data?.code,
    })
  }
  return data
}

export const realApi = {
  health: () => request('/health'),

  createConversation: () => request('/conversations', { method: 'POST' }),
  getConversation: (sessionId) => request(`/conversations/${sessionId}`),
  getConversationTaskReferences: (sessionId) => request(`/conversations/${sessionId}/tasks`),
  deleteConversation: (sessionId) => request(`/conversations/${sessionId}`, { method: 'DELETE' }),
  classifyAssistantRequest: (message, sessionId) =>
    request('/assistant/classify', {
      method: 'POST',
      body: sessionId ? { message, session_id: sessionId } : { message },
    }),
  assistantRequest: (message, sessionId) =>
    request('/assistant/requests', {
      method: 'POST',
      body: sessionId ? { message, session_id: sessionId } : { message },
    }),
  sendConversationMessage: (sessionId, message, timezone) =>
    request(`/conversations/${sessionId}/messages`, {
      method: 'POST',
      body: {
        message,
        request_id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`,
        timezone: timezone || undefined,
      },
    }),

  createTask: (userRequest, autoExecute = false) =>
    request('/tasks', { method: 'POST', body: { user_request: userRequest, auto_execute: autoExecute } }),
  getTask: (taskId) => request(`/tasks/${taskId}`),
  getTaskEvents: (taskId) => request(`/tasks/${taskId}/events`),
  executeTask: (taskId) => request(`/tasks/${taskId}/execute`, { method: 'POST' }),
  cancelTask: (taskId) => request(`/tasks/${taskId}/cancel`, { method: 'POST' }),

  // Implemented by Member 3 (feature/orchestration-engine): body {"decision": "APPROVED" | "REJECTED"}.
  // Resolving an approval does NOT resume execution; callers must call executeTask afterwards.
  decideApproval: (taskId, approvalId, decision) =>
    request(`/tasks/${taskId}/approvals/${approvalId}`, { method: 'POST', body: { decision: String(decision).toUpperCase() } }),

  // PROVISIONAL request bodies: no shared request model yet.
  startRecording: ({ name, startUrl }) =>
    request('/browser/record/start', { method: 'POST', body: { name, start_url: startUrl } }),
  stopRecording: () => request('/browser/record/stop', { method: 'POST' }),
  listWorkflows: () => request('/browser/workflows'),
  replayWorkflow: (workflowId, parameters = {}) =>
    request(`/browser/replay/${workflowId}`, { method: 'POST', body: { parameters } }),

  uploadDocument: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    return request('/documents/upload', { method: 'POST', formData })
  },
  listDocuments: () => request('/documents'),
  searchDocuments: (query) => request('/documents/search', { method: 'POST', body: { query } }),

  listIntegrations: () => request('/integrations'),
}
