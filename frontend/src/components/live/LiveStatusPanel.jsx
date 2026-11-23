import { DISPLAY_STATUS_LABEL } from '../../live/liveState'
import { formatTime } from '../../lib/format'
import { Card, StatusBadge } from '../ui'

function latestStepVerification(plan) {
  for (let i = plan.length - 1; i >= 0; i--) {
    const v = plan[i].verification
    if (v && typeof v.verified === 'boolean') return { status: v.verified ? 'passed' : 'failed', detail: `${plan[i].step_id}: ${plan[i].description}` }
  }
  return null
}

function lastActivity(live, taskEvents) {
  const lastTask = taskEvents?.length ? taskEvents[taskEvents.length - 1] : null
  const taskTime = lastTask ? Date.parse(lastTask.timestamp) : NaN
  const liveLast = live.events.length ? live.events[live.events.length - 1] : null
  if (liveLast && (Number.isNaN(taskTime) || liveLast.time >= taskTime)) return { time: liveLast.timestamp, text: liveLast.description }
  if (lastTask) return { time: lastTask.timestamp, text: lastTask.message }
  return null
}

function Row({ label, children }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2 py-1.5 text-xs">
      <dt className="text-slate-400">{label}</dt>
      <dd className="min-w-0 break-words text-slate-100">{children}</dd>
    </div>
  )
}

export default function LiveStatusPanel({ task, taskEvents, live, displayStatus, connectionStatus, warnings }) {
  const stepIndex = task.plan.findIndex((s) => s.step_id === (task.current_step ?? live.currentAction?.step_id))
  const step = stepIndex >= 0 ? task.plan[stepIndex] : null
  const verification = live.verification ?? latestStepVerification(task.plan)
  const activity = lastActivity(live, taskEvents)

  return (
    <Card title="Task status" actions={<StatusBadge status={displayStatus} label={DISPLAY_STATUS_LABEL[displayStatus]} />}>
      <dl className="divide-y divide-slate-800" data-testid="live-status">
        <Row label="Task">{task.user_request}</Row>
        <Row label="Current action">{live.currentAction?.description ?? step?.description ?? '—'}</Row>
        <Row label="Step">
          {step ? `${stepIndex + 1} of ${task.plan.length}` : task.plan.length ? `— of ${task.plan.length}` : 'Unknown'}
          {step && <code className="ml-2 text-violet-300">{step.tool_name}</code>}
        </Row>
        <Row label="Execution">
          <StatusBadge status={displayStatus} label={DISPLAY_STATUS_LABEL[displayStatus]} />
          <span className="ml-2 text-slate-500">backend: {task.status.replaceAll('_', ' ')}</span>
        </Row>
        <Row label="Browser link">
          <StatusBadge status={connectionStatus} />
          {live.page?.url && <span className="ml-2 break-all text-slate-400">{live.page.url}</span>}
        </Row>
        <Row label="Last activity">{activity ? `${formatTime(activity.time)} · ${activity.text}` : 'None yet'}</Row>
        <Row label="Verification">
          {verification ? (
            <>
              <StatusBadge status={verification.status} />
              {verification.detail && <span className="ml-2 text-slate-400">{verification.detail}</span>}
            </>
          ) : (
            <span className="text-slate-400">Not verified yet</span>
          )}
        </Row>
      </dl>

      {warnings.length > 0 && (
        <ul className="mt-3 space-y-1.5" data-testid="live-warnings">
          {warnings.map((w) => (
            <li key={w.text} className={`rounded-md px-2.5 py-1.5 text-xs ${w.level === 'error' ? 'border border-rose-500/40 bg-rose-500/10 text-rose-200' : 'border border-amber-500/40 bg-amber-500/10 text-amber-200'}`}>
              {w.text}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}