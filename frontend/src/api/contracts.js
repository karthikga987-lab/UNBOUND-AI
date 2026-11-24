// Mirrors backend/app/schemas/contracts.py (owned by Member 3).
// Keep these values identical to the backend enums. Never invent fields here.

export const TaskStatus = Object.freeze({
  CREATED: 'created',
  PLANNING: 'planning',
  AWAITING_APPROVAL: 'awaiting_approval',
  EXECUTING: 'executing',
  VERIFYING: 'verifying',
  COMPLETED: 'completed',
  FAILED: 'failed',
  NEEDS_ATTENTION: 'needs_attention',
  CANCELLED: 'cancelled',
})

export const StepStatus = Object.freeze({
  PENDING: 'pending',
  RUNNING: 'running',
  AWAITING_APPROVAL: 'awaiting_approval',
  COMPLETED: 'completed',
  FAILED: 'failed',
  SKIPPED: 'skipped',
})

export const ApprovalStatus = Object.freeze({
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
  CANCELLED: 'cancelled',
})

export const EvidenceSourceType = Object.freeze({
  BROWSER: 'browser',
  DOCUMENT: 'document',
  CONTEXT: 'context',
  TOOL: 'tool',
  WORKFLOW: 'workflow',
  REPORT: 'report',
})

const TERMINAL = new Set([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
export const isTerminal = (status) => TERMINAL.has(status)
// NEEDS_ATTENTION can mean missing verification or an uncertain external
// outcome; neither is safe to retry through the generic execute control.
export const canExecute = (status) => status === TaskStatus.CREATED
export const canCancel = (status) => !isTerminal(status)
