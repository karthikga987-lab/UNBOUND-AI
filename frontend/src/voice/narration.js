import { TaskStatus } from '../api/contracts'

// Turns real task data into short spoken lines. Every sentence here is built from
// fields the backend actually returned — nothing is invented for effect.

const lowerFirst = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : s)
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const asArray = (v) => (Array.isArray(v) ? v : v ? [v] : [])

export function describePlan(task, autoExecute) {
  const steps = task?.plan ?? []
  if (!steps.length) return autoExecute ? 'On it.' : 'Task created. Say "run it" when you are ready.'
  const [first, second] = steps
  let line = `I've drafted a ${steps.length}-step plan. First, I'll ${lowerFirst(first.description)}`
  if (second) line += `, then ${lowerFirst(second.description)}`
  if (steps.length > 2) line += `, plus ${plural(steps.length - 2, 'more step')}`
  line += '.'
  const gated = steps.filter((s) => s.requires_approval).length
  if (gated) line += ` ${gated === 1 ? 'One step needs' : `${gated} steps need`} your approval before I act.`
  line += autoExecute || task.status === TaskStatus.EXECUTING ? ' Starting now.' : ' Say "run it" when you are ready.'
  return line
}

export function describeResult(task) {
  const result = task?.result
  if (!result) return 'There are no results yet.'
  const summary = typeof result === 'string' ? result : result.summary
  const facts = asArray(result.verified_facts).length
  const conflicts = asArray(result.conflicts).length
  const parts = [summary || 'The task finished without a summary.']
  if (facts || conflicts) {
    const found = [facts && `verified ${plural(facts, 'fact')}`, conflicts && `found ${plural(conflicts, 'conflict')} between sources`].filter(Boolean)
    parts.push(`I ${found.join(' and ')}.`)
  }
  parts.push('The full details are on screen.')
  return parts.join(' ')
}

export function describeStatus(task) {
  if (!task) return 'Nothing is running right now. Tell me what you need.'
  const steps = task.plan ?? []
  const step = steps.find((s) => s.step_id === task.current_step)
  const done = task.completed_steps?.length ?? 0
  switch (task.status) {
    case TaskStatus.CREATED:
      return `The plan is ready with ${plural(steps.length, 'step')}. Say "run it" to start.`
    case TaskStatus.EXECUTING:
    case TaskStatus.VERIFYING:
      return `${done} of ${steps.length} steps done.${step ? ` Currently: ${lowerFirst(step.description)}.` : ''}`
    case TaskStatus.AWAITING_APPROVAL:
      return approvalLine(task)
    case TaskStatus.COMPLETED:
      return describeResult(task)
    case TaskStatus.FAILED:
      return `The task failed.${task.error ? ` ${errorText(task.error)}` : ''}`
    default:
      return `The task is ${String(task.status).replaceAll('_', ' ')}.`
  }
}

function approvalLine(task) {
  const pending = task.pending_approvals?.[0]
  const what = pending?.description ?? task.plan?.find((s) => s.step_id === task.current_step)?.description
  return `I need your go-ahead${what ? ` to ${lowerFirst(what)}` : ''}. Use Approve or Reject on screen.`
}

function errorText(error) {
  return typeof error === 'string' ? error : (error?.message ?? '')
}

/**
 * What to say when the active task moves from `prev` to `next`.
 * Returns { text, replaceable } or null. Step updates are replaceable so a fast
 * task doesn't build a backlog of stale narration.
 */
export function narrateChange(prev, next) {
  if (!next) return null
  const statusChanged = prev?.status !== next.status
  if (statusChanged) {
    switch (next.status) {
      case TaskStatus.AWAITING_APPROVAL:
        return { text: approvalLine(next) }
      case TaskStatus.COMPLETED:
        return { text: `All done. ${describeResult(next)}` }
      case TaskStatus.FAILED:
        return { text: `The task failed.${next.error ? ` ${errorText(next.error)}` : ''} Details are on screen.` }
      case TaskStatus.NEEDS_ATTENTION:
        return { text: 'This task needs your attention. Details are on screen.' }
      case TaskStatus.CANCELLED:
        return { text: 'Task cancelled. Anything gathered so far is kept on screen.' }
      default:
        break
    }
  }
  if (next.current_step && next.current_step !== prev?.current_step && next.status === TaskStatus.EXECUTING) {
    const steps = next.plan ?? []
    const index = steps.findIndex((s) => s.step_id === next.current_step)
    const step = steps[index]
    if (step) return { text: `Step ${index + 1} of ${steps.length}: ${lowerFirst(step.description)}.`, replaceable: true }
  }
  return null
}

const COMMANDS = [
  ['run', /^(ok(ay)?,? )?(run|start|execute|go ahead|do it|proceed|begin)( it| the task| that| now)?( please)?$/],
  ['cancel', /^(cancel|stop|abort)( the| this)? task( please)?$/],
  ['approve', /^(approve|reject|deny)( it| that| this| the step)?$/],
  ['results', /^(read|tell me|what('s| is| are))( me)? (the )?(results?|summary|findings)$/],
  ['status', /^(status|what's the status|what is the status|how('s| is) it going|progress|what are you doing)$/],
  ['stop', /^(goodbye|that's all|stop listening|end voice( mode)?)$/],
]

/** Recognises a short spoken command; anything else becomes a new task. */
export function parseCommand(text) {
  const clean = String(text ?? '').toLowerCase().replace(/[.!?]+$/g, '').replace(/\s+/g, ' ').trim()
  for (const [name, re] of COMMANDS) if (re.test(clean)) return name
  return null
}
