// Real backend client. Endpoints follow docs/API_CONTRACT.md.
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message, { status, code } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function formatDetail(detail) {
  if (!detail) return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d?.msg ?? JSON.stringify(d)).join('; ')
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
    throw new ApiError(`Cannot reach the backend at ${API_BASE_URL}. Is it running?`, { status: 0, code: 'network_error' })
  }
  const text = await response.text()
  let data = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }
  if (!response.ok) {
    throw new ApiError(formatDetail(data?.detail) ?? `Request failed (${response.status})`, {
      status: response.status,
      code: data?.code,
    })
  }
  return data
}

export const realApi = {
  health: () => request('/health'),

  createTask: (userRequest, autoExecute = false) =>
    request('/tasks', { method: 'POST', body: { user_request: userRequest, auto_execute: autoExecute } }),
  getTask: (taskId) => request(`/tasks/${taskId}`),
  getTaskEvents: (taskId) => request(`/tasks/${taskId}/events`),
  executeTask: (taskId) => request(`/tasks/${taskId}/execute`, { method: 'POST' }),
  cancelTask: (taskId) => request(`/tasks/${taskId}/cancel`, { method: 'POST' }),

  // PROVISIONAL: approval endpoint is not in API_CONTRACT.md yet. Confirm path/body with Member 3.
  decideApproval: (taskId, approvalId, decision) =>
    request(`/tasks/${taskId}/approvals/${approvalId}`, { method: 'POST', body: { decision } }),

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