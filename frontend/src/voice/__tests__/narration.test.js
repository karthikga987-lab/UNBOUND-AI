import { describe, expect, it } from 'vitest'
import { describePlan, describeResult, describeStatus, narrateChange, parseCommand } from '../narration'

const plan = [
  { step_id: 's1', description: 'Open the reports website', requires_approval: false },
  { step_id: 's2', description: 'Replay saved workflow', requires_approval: true },
  { step_id: 's3', description: 'Search uploaded documents', requires_approval: false },
]
const task = (over = {}) => ({ task_id: 't1', status: 'created', plan, current_step: null, completed_steps: [], pending_approvals: [], result: null, ...over })

describe('narration', () => {
  it('describes the real plan', () => {
    const line = describePlan(task(), false)
    expect(line).toContain('3-step plan')
    expect(line).toContain("I'll open the reports website, then replay saved workflow, plus 1 more step")
    expect(line).toContain('One step needs your approval')
    expect(line).toContain('Say "run it"')
    expect(describePlan(task(), true)).toContain('Starting now.')
  })

  it('reads the actual result', () => {
    const done = task({ status: 'completed', result: { summary: 'Revenue differs.', verified_facts: [{}, {}], conflicts: [{}] } })
    expect(describeResult(done)).toBe('Revenue differs. I verified 2 facts and found 1 conflict between sources. The full details are on screen.')
  })

  it('narrates step and status changes from task data', () => {
    const a = task({ status: 'executing' })
    const b = task({ status: 'executing', current_step: 's3' })
    expect(narrateChange(a, b)).toEqual({ text: 'Step 3 of 3: search uploaded documents.', replaceable: true })
    const c = task({ status: 'awaiting_approval', current_step: 's2', pending_approvals: [{ description: 'Replay saved workflow' }] })
    expect(narrateChange(b, c).text).toBe('I need your go-ahead to replay saved workflow. Use Approve or Reject on screen.')
    expect(narrateChange(c, c)).toBeNull()
  })

  it('reports status honestly when nothing is running', () => {
    expect(describeStatus(null)).toMatch(/Nothing is running/)
  })

  it('does not imply a needs-attention task can safely be resumed', () => {
    const line = describeStatus(task({ status: 'needs_attention', error: 'Outcome unknown' }))
    expect(line).toMatch(/needs your attention/i)
    expect(line).toMatch(/will not retry/i)
  })

  it.each([
    ['Run it.', 'run'],
    ['go ahead', 'run'],
    ['Cancel the task', 'cancel'],
    ["What's the status?", 'status'],
    ['read the results', 'results'],
    ['approve', 'approve'],
    ['Find every mention of Q3 revenue', null],
    ['run a report on sales', null],
  ])('parses %j', (text, cmd) => {
    expect(parseCommand(text)).toBe(cmd)
  })
})
