// ⚠️ TEMPORARY MOCK ADAPTER: used only while backend endpoints are not ready.
// Task/step/approval/event/evidence/workflow objects match backend/app/schemas/contracts.py
// exactly, so the UI needs no changes when api/index.js switches to the real client.
// Documents, search results, integrations and recording responses are PROVISIONAL
// (no shared contract yet); update them when Members 2/3/4 publish one.
import { ApiError } from './client'
import { ApprovalStatus, StepStatus, TaskStatus, isTerminal } from './contracts'

const STEP_INTERVAL_MS = 1400
const uuid = () => crypto.randomUUID()
const now = () => new Date().toISOString()
const wait = (ms = 250) => new Promise((resolve) => setTimeout(resolve, ms))
const copy = (value) => structuredClone(value)

const tasks = new Map()
let recording = null

let workflows = [
  {
    workflow_id: uuid(),
    name: 'Download latest report',
    description: 'Opens the reports portal and downloads the newest published report.',
    start_url: 'https://reports.example.com/',
    parameters: [
      { name: 'report_period', type: 'string', required: false, default: 'latest', description: 'Which report to fetch' },
    ],
    steps: [
      { step_id: 'w1', action: 'navigate', target: { url: 'https://reports.example.com/' }, input: {}, expected_outcome: 'Reports page loaded' },
      { step_id: 'w2', action: 'click', target: { text: 'Latest report' }, input: {}, expected_outcome: 'Report page opened' },
      { step_id: 'w3', action: 'download', target: { text: 'Download PDF' }, input: {}, expected_outcome: 'PDF downloaded' },
    ],
    created_at: now(),
    updated_at: now(),
  },
]

let documents = [
  { document_id: uuid(), filename: 'finance_summary.pdf', content_type: 'application/pdf', size_bytes: 248312, status: 'ingested', uploaded_at: now() },
  { document_id: uuid(), filename: 'regional_sales.xlsx', content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size_bytes: 52904, status: 'ingested', uploaded_at: now() },
]

const PASSAGES = [
  { filename: 'finance_summary.pdf', location: 'page 2', excerpt: 'Q3 2026 revenue (internal estimate): $4.0M, up 12% quarter over quarter.' },
  { filename: 'finance_summary.pdf', location: 'page 4', excerpt: 'Operating costs for Q3 2026 were $2.6M, driven by infrastructure spend.' },
  { filename: 'regional_sales.xlsx', location: 'Sheet1!B7', excerpt: 'APAC region contributed $1.3M of Q3 revenue.' },
]

const TOOL_EVIDENCE = {
  'browser.navigate': { source_type: 'browser', source_name: 'reports.example.com', source_url: 'https://reports.example.com/', file_path: null, excerpt: 'Quarterly Reports: latest is Q3 2026', supports_claim: 'The latest published report on the website is Q3 2026.' },
  'browser.replay': { source_type: 'workflow', source_name: 'Q3-2026-report.pdf', source_url: 'https://reports.example.com/files/q3-2026.pdf', file_path: null, excerpt: 'Total revenue for Q3 2026: $4.2M', supports_claim: 'The website report states Q3 2026 revenue of $4.2M.' },
  'document.search': { source_type: 'document', source_name: 'finance_summary.pdf', source_url: null, file_path: 'uploads/finance_summary.pdf', excerpt: 'Q3 2026 revenue (internal estimate): $4.0M', supports_claim: 'The uploaded document states Q3 2026 revenue of $4.0M.' },
}

function getEntry(taskId) {
  const entry = tasks.get(taskId)
  if (!entry) throw new ApiError('Task not found.', { status: 404, code: 'task_not_found' })
  return entry
}

function addEvent(entry, { step_id = null, event_type, message, status, evidence_ids = [] }) {
  entry.events.push({ event_id: uuid(), task_id: entry.task.task_id, step_id, event_type, message, status, timestamp: now(), evidence_ids })
  entry.task.updated_at = now()
}

function makeStep(step_id, tool_name, description, input, expected_outcome, requires_approval = false) {
  return { step_id, tool_name, description, input, status: StepStatus.PENDING, requires_approval, expected_outcome, verification: null, error: null }
}

function defaultPlan(userRequest) {
  const wf = workflows[0]
  return [
    makeStep('step_1', 'browser.navigate', 'Open the reports website', { url: 'https://reports.example.com/' }, 'Reports page is loaded'),
    makeStep('step_2', 'browser.replay', `Replay saved workflow "${wf?.name ?? 'Download latest report'}"`, { workflow_id: wf?.workflow_id ?? null, parameters: { report_period: 'latest' } }, 'Latest report PDF is downloaded', true),
    makeStep('step_3', 'document.search', 'Search uploaded documents for related figures', { query: userRequest }, 'Relevant passages retrieved with sources'),
    makeStep('step_4', 'context.relate', 'Compare website figures with document figures', {}, 'Matches and conflicts identified'),
    makeStep('step_5', 'report.generate', 'Generate an evidence-backed report', { format: 'markdown' }, 'Report artifact created'),
  ]
}

function createEntry(userRequest, plan) {
  const task = {
    task_id: uuid(), user_request: userRequest, status: TaskStatus.CREATED, plan, current_step: null,
    completed_steps: [], pending_approvals: [], evidence: [], artifacts: [], result: null, error: null,
    created_at: now(), updated_at: now(),
  }
  const entry = { task, events: [], approvedSteps: new Set(), lastAdvance: 0 }
  tasks.set(task.task_id, entry)
  addEvent(entry, { event_type: 'task.created', message: 'Task created from natural-language request', status: 'created' })
  addEvent(entry, { event_type: 'task.planned', message: `Planned ${plan.length} step(s) using registered tools`, status: 'planning' })
  return entry
}

function startExecution(entry) {
  entry.task.status = TaskStatus.EXECUTING
  entry.lastAdvance = 0
  addEvent(entry, { event_type: 'task.executing', message: 'Execution started', status: 'running' })
}

function buildReport(task) {
  const lines = ['# UNBOUND AI report', '', `**Request:** ${task.user_request}`, '', '## Summary', task.result.summary, '', '## Verified facts']
  task.result.verified_facts.forEach((f) => lines.push(`- ${f.statement}`))
  if (task.result.inferences.length) {
    lines.push('', '## Inferences (not verified)')
    task.result.inferences.forEach((i) => lines.push(`- ${i.statement}`))
  }
  lines.push('', '## Sources')
  task.evidence.forEach((e, i) => lines.push(`${i + 1}. ${e.source_name}: ${e.source_url ?? e.file_path ?? ''} ("${e.excerpt ?? ''}")`))
  return lines.join('\n')
}

function finish(entry) {
  const { task } = entry
  const done = (tool) => task.plan.some((s) => s.tool_name === tool && s.status === StepStatus.COMPLETED)
  const website = task.evidence.find((e) => e.source_type === 'workflow')
  const doc = task.evidence.find((e) => e.source_type === 'document')
  const compared = done('context.relate') && website && doc
  const skipped = task.plan.filter((s) => s.status === StepStatus.SKIPPED)

  task.status = TaskStatus.COMPLETED
  task.current_step = null
  task.result = {
    summary: compared
      ? 'The website report and your uploaded document disagree on Q3 2026 revenue ($4.2M vs $4.0M). Both sources are cited; the discrepancy is not resolved automatically.'
      : `Finished ${task.completed_steps.length} of ${task.plan.length} step(s).`,
    verified_facts: task.evidence.map((e) => ({ statement: e.supports_claim, evidence_ids: [e.evidence_id] })),
    inferences: compared
      ? [{ statement: 'The gap may exist because the document is a pre-close internal estimate. This is an inference, not a verified fact.', evidence_ids: [website.evidence_id, doc.evidence_id] }]
      : [],
    conflicts: compared
      ? [{ statement: 'Website reports $4.2M Q3 revenue; uploaded document reports $4.0M.', evidence_ids: [website.evidence_id, doc.evidence_id] }]
      : [],
    missing_information: skipped.map((s) => `"${s.description}" was skipped because approval was rejected.`),
  }
  if (done('report.generate')) {
    task.artifacts.push({ artifact_id: uuid(), name: 'unbound-report.md', type: 'report', mime_type: 'text/markdown', content: buildReport(task) })
  }
  addEvent(entry, { event_type: 'task.completed', message: 'Task completed; all executed steps verified', status: 'completed' })
}

function advance(entry) {
  const { task } = entry
  if (task.status !== TaskStatus.EXECUTING) return
  if (Date.now() - entry.lastAdvance < STEP_INTERVAL_MS) return
  entry.lastAdvance = Date.now()

  const step = task.plan.find((s) => s.status === StepStatus.RUNNING || s.status === StepStatus.PENDING)
  if (!step) {
    finish(entry)
    return
  }

  if (step.status === StepStatus.RUNNING) {
    const template = TOOL_EVIDENCE[step.tool_name]
    const evidence = template ? { evidence_id: uuid(), task_id: task.task_id, retrieved_at: now(), ...template } : null
    if (evidence) task.evidence.push(evidence)
    const evidenceIds = evidence ? [evidence.evidence_id] : []
    step.status = StepStatus.COMPLETED
    step.verification = { verified: true, method: 'mock', checked: step.expected_outcome, evidence_ids: evidenceIds }
    task.completed_steps.push(step.step_id)
    task.current_step = null
    addEvent(entry, { step_id: step.step_id, event_type: 'step.completed', message: `Verified: ${step.expected_outcome}`, status: 'completed', evidence_ids: evidenceIds })
    return
  }

  task.current_step = step.step_id
  if (step.requires_approval && !entry.approvedSteps.has(step.step_id)) {
    step.status = StepStatus.AWAITING_APPROVAL
    task.status = TaskStatus.AWAITING_APPROVAL
    task.pending_approvals.push({
      approval_id: uuid(), task_id: task.task_id, step_id: step.step_id, tool_name: step.tool_name,
      description: step.description, arguments: copy(step.input), status: ApprovalStatus.PENDING, created_at: now(),
    })
    addEvent(entry, { step_id: step.step_id, event_type: 'approval.requested', message: `Approval required: ${step.description}`, status: 'awaiting_approval' })
    return
  }
  step.status = StepStatus.RUNNING
  addEvent(entry, { step_id: step.step_id, event_type: 'step.started', message: step.description, status: 'running' })
}

export const mockApi = {
  async health() {
    await wait(100)
    return { status: 'ok' }
  },
  async s3SyncStatus() { return { enabled: false, status: 'simulated', pending_count: 0, retry_count: 0 } },
  async listS3Backups() { return [] },
  async syncToS3() { throw new Error('S3 synchronization is unavailable in simulated mode.') },
  async restoreTaskBackup() { throw new Error('S3 restore is unavailable in simulated mode.') },

  async listTasks() {
    await wait(80)
    return [...tasks.values()].map((entry) => copy(entry.task))
  },

  async getConversationTaskReferences(sessionId) {
    return { session_id: sessionId, available_task_ids: [], unavailable_task_ids: [] }
  },

  async createTask(userRequest, autoExecute = false) {
    await wait()
    const text = String(userRequest ?? '').trim()
    if (!text) throw new ApiError('Please describe the task.', { status: 422 })
    const entry = createEntry(text, defaultPlan(text))
    if (autoExecute) startExecution(entry)
    return copy(entry.task)
  },

  async getTask(taskId) {
    await wait(120)
    const entry = getEntry(taskId)
    advance(entry)
    return copy(entry.task)
  },

  async getTaskEvents(taskId) {
    await wait(60)
    return copy(getEntry(taskId).events)
  },

  async executeTask(taskId) {
    await wait()
    const entry = getEntry(taskId)
    if (isTerminal(entry.task.status)) throw new ApiError('Task has already finished.', { status: 409 })
    if (entry.task.status === TaskStatus.NEEDS_ATTENTION) {
      throw new ApiError('This task needs review and cannot be resumed automatically.', { status: 409, code: 'task_needs_attention' })
    }
    if (entry.task.status === TaskStatus.CREATED) startExecution(entry)
    else throw new ApiError('Task is not in a runnable state.', { status: 409, code: 'task_not_runnable' })
    return copy(entry.task)
  },

  async cancelTask(taskId) {
    await wait()
    const entry = getEntry(taskId)
    const { task } = entry
    if (isTerminal(task.status)) throw new ApiError('Task has already finished.', { status: 409 })
    task.plan.forEach((s) => {
      if (s.status === StepStatus.RUNNING) {
        s.status = StepStatus.FAILED
        s.error = 'Cancelled by user before verification'
      } else if (s.status === StepStatus.AWAITING_APPROVAL) {
        s.status = StepStatus.SKIPPED
      }
    })
    task.pending_approvals = []
    task.status = TaskStatus.CANCELLED
    task.current_step = null
    addEvent(entry, { event_type: 'task.cancelled', message: 'Task cancelled by user. Completed steps and evidence were kept.', status: 'cancelled' })
    return copy(task)
  },

  async decideApproval(taskId, approvalId, decision) {
    await wait()
    const entry = getEntry(taskId)
    const { task } = entry
    const approval = task.pending_approvals.find((a) => a.approval_id === approvalId)
    if (!approval) throw new ApiError('Approval not found or already resolved.', { status: 404 })
    task.pending_approvals = task.pending_approvals.filter((a) => a.approval_id !== approvalId)
    const step = task.plan.find((s) => s.step_id === approval.step_id)
    if (decision === ApprovalStatus.APPROVED) {
      entry.approvedSteps.add(step.step_id)
      step.status = StepStatus.PENDING
      addEvent(entry, { step_id: step.step_id, event_type: 'approval.approved', message: 'Approval recorded; explicitly run the plan to continue.', status: 'created' })
      task.status = TaskStatus.CREATED
    } else {
      step.status = StepStatus.SKIPPED
      task.current_step = null
      addEvent(entry, { step_id: step.step_id, event_type: 'approval.rejected', message: `Rejected, step skipped: ${step.description}`, status: 'cancelled' })
      task.status = TaskStatus.NEEDS_ATTENTION
    }
    return copy(task)
  },

  async startRecording({ name, startUrl }) {
    await wait()
    if (recording) throw new ApiError('A recording is already in progress.', { status: 409 })
    if (!/^https?:\/\//i.test(startUrl ?? '')) throw new ApiError('Start URL must begin with http:// or https://', { status: 422 })
    recording = { recording_id: uuid(), name, start_url: startUrl, status: 'recording', started_at: now() }
    return copy(recording)
  },

  async stopRecording() {
    await wait(500)
    if (!recording) throw new ApiError('No active recording.', { status: 409 })
    const wf = {
      workflow_id: uuid(),
      name: recording.name || 'Untitled workflow',
      description: 'Recorded from your demonstration (mock).',
      start_url: recording.start_url,
      parameters: [],
      steps: [{ step_id: 'w1', action: 'navigate', target: { url: recording.start_url }, input: {}, expected_outcome: 'Start page loaded' }],
      created_at: now(),
      updated_at: now(),
    }
    workflows = [wf, ...workflows]
    recording = null
    return copy(wf)
  },

  async listWorkflows() {
    await wait()
    return copy(workflows)
  },

  async replayWorkflow(workflowId, parameters = {}) {
    await wait()
    const wf = workflows.find((w) => w.workflow_id === workflowId)
    if (!wf) throw new ApiError('Workflow not found.', { status: 404 })
    const missing = wf.parameters.filter((p) => p.required && String(parameters[p.name] ?? '').trim() === '')
    if (missing.length) throw new ApiError(`Missing required parameter(s): ${missing.map((p) => p.name).join(', ')}`, { status: 422 })
    const plan = [
      makeStep('step_1', 'browser.navigate', `Open ${wf.start_url}`, { url: wf.start_url }, 'Start page is loaded'),
      makeStep('step_2', 'browser.replay', `Replay workflow "${wf.name}"`, { workflow_id: wf.workflow_id, parameters }, wf.steps.at(-1)?.expected_outcome ?? 'Workflow completed', true),
    ]
    const entry = createEntry(`Replay workflow "${wf.name}"`, plan)
    startExecution(entry)
    return copy(entry.task)
  },

  async uploadDocument(file) {
    await wait(700)
    if (!/\.(pdf|docx|txt|csv|xlsx)$/i.test(file.name)) {
      throw new ApiError('Unsupported file type. Use PDF, DOCX, TXT, CSV or XLSX.', { status: 415 })
    }
    const doc = { document_id: uuid(), filename: file.name, content_type: file.type || 'application/octet-stream', size_bytes: file.size, status: 'ingested', uploaded_at: now() }
    documents = [doc, ...documents]
    return copy(doc)
  },

  async listDocuments() {
    await wait()
    return copy(documents)
  },

  async searchDocuments(query) {
    await wait(400)
    const q = String(query ?? '').trim().toLowerCase()
    if (!q) throw new ApiError('Enter a search query.', { status: 422 })
    const words = q.split(/\s+/).filter((w) => w.length > 2)
    const results = PASSAGES.filter((p) => {
      const text = p.excerpt.toLowerCase()
      return text.includes(q) || words.some((w) => text.includes(w))
    }).map((p, i) => ({ ...p, score: Number((0.92 - i * 0.07).toFixed(2)) }))
    return { query, results }
  },

  async listIntegrations() {
    await wait()
    return [
      { integration_id: 'browser', name: 'Browser automation', status: 'available', description: 'Navigate, record and replay website workflows.' },
      { integration_id: 'documents', name: 'Document store', status: 'available', description: 'PDF, DOCX and TXT ingestion with source provenance.' },
      { integration_id: 'gdrive', name: 'Google Drive (MCP)', status: 'not_configured', description: 'Connect to search Drive files.' },
      { integration_id: 'email', name: 'Email (MCP)', status: 'not_configured', description: 'Sending email always requires approval.' },
    ]
  },
}
