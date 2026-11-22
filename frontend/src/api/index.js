// The ONLY place that decides mock vs real backend. Components always import `api` from here.
import { realApi } from './client'
import { mockApi } from './mockAdapter'

const env = import.meta.env

// Mock everything by default. Set VITE_USE_MOCK=false in frontend/.env.local to use the backend.
export const USE_MOCK = env.VITE_USE_MOCK !== 'false'

// With the real backend on, list areas whose endpoints are not ready yet, e.g.
// VITE_MOCK_AREAS=approvals,workflows,documents,integrations
const MOCK_AREAS = new Set((env.VITE_MOCK_AREAS ?? '').split(',').map((s) => s.trim()).filter(Boolean))

const AREA = {
  health: 'health',
  createTask: 'tasks', getTask: 'tasks', getTaskEvents: 'tasks', executeTask: 'tasks', cancelTask: 'tasks',
  decideApproval: 'approvals',
  startRecording: 'workflows', stopRecording: 'workflows', listWorkflows: 'workflows', replayWorkflow: 'workflows',
  uploadDocument: 'documents', listDocuments: 'documents', searchDocuments: 'documents',
  listIntegrations: 'integrations',
}

export const isMocked = (area) => USE_MOCK || MOCK_AREAS.has(area)

export const api = Object.fromEntries(
  Object.keys(AREA).map((name) => [name, (...args) => (isMocked(AREA[name]) ? mockApi : realApi)[name](...args)]),
)

export { ApiError, API_BASE_URL } from './client'