import { canCancel, canExecute, TaskStatus } from '../api/contracts'
import { formatTime } from '../lib/format'
import ApprovalCard from './ApprovalCard'
import { Button, Card, ErrorState, JsonBlock, StatusBadge } from './ui'

const STEP_ICON = {
  pending: { icon: '○', cls: 'border-slate-700 text-slate-500' },
  running: { icon: '◐', cls: 'border-blue-500 text-blue-300 animate-pulse' },
  awaiting_approval: { icon: '!', cls: 'border-amber-500 text-amber-300' },
  completed: { icon: '✓', cls: 'border-emerald-500 text-emerald-300' },
  failed: { icon: '✕', cls: 'border-rose-500 text-rose-300' },
  skipped: { icon: '–', cls: 'border-zinc-600 text-zinc-400' },
}

export default function TimelinePanel({ task, events, error, busy, actionError, onExecute, onCancel, onDecide, onOpenLive }) {
  const counts = task.plan.reduce((acc, s) => ({ ...acc, [s.status]: (acc[s.status] ?? 0) + 1 }), {})
  const stepIds = new Set(task.plan.map((s) => s.step_id))
  const approvalsByStep = Object.fromEntries(task.pending_approvals.map((a) => [a.step_id, a]))
  const orphanApprovals = task.pending_approvals.filter((a) => !stepIds.has(a.step_id))
  const waiting = task.pending_approvals.length

  return (
    <Card
      title="Live execution"
      subtitle={task.user_request}
      actions={
        <>
          <StatusBadge status={task.status} />
          {onOpenLive && (
            <Button size="sm" variant="secondary" onClick={onOpenLive}>
              Watch live
            </Button>
          )}
          {canExecute(task.status) && (
            <Button size="sm" onClick={onExecute} loading={busy === 'execute'}>
              {task.status === TaskStatus.CREATED ? 'Run plan' : 'Resume'}
            </Button>
          )}
          {canCancel(task.status) && (
            <Button size="sm" variant="danger" onClick={onCancel} loading={busy === 'cancel'}>
              Stop task
            </Button>
          )}
        </>
      }
    >
      {waiting > 0 && (
        <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
          ⏸ Waiting for your approval on {waiting} sensitive action{waiting > 1 ? 's' : ''}. Nothing runs until you decide.
        </div>
      )}
      {task.status === TaskStatus.CREATED && (
        <p className="mb-4 rounded-lg border border-slate-700 bg-slate-800/50 px-3 py-2 text-sm text-slate-300">
          Review the plan below, then click <b>Run plan</b>.
        </p>
      )}
      {task.error && <div className="mb-4"><ErrorState error={task.error} compact title="Task error" /></div>}
      {actionError && <div className="mb-4"><ErrorState error={actionError} compact title="Action failed" /></div>}
      {error && <div className="mb-4"><ErrorState error={error} compact title="Connection problem, retrying" /></div>}

      <div className="mb-4 grid grid-cols-2 gap-2 text-center text-xs sm:grid-cols-5">
        {[
          ['Completed', counts.completed],
          ['Running', counts.running],
          ['Awaiting approval', counts.awaiting_approval],
          ['Failed', counts.failed],
          ['Pending', counts.pending],
        ].map(([label, n]) => (
          <div key={label} className="rounded-lg bg-slate-950/60 px-2 py-2 ring-1 ring-slate-800">
            <div className="text-lg font-semibold text-slate-100">{n ?? 0}</div>
            <div className="text-slate-400">{label}</div>
          </div>
        ))}
      </div>

      {orphanApprovals.map((a) => (
        <div key={a.approval_id} className="mb-3">
          <ApprovalCard approval={a} busy={busy} onDecide={onDecide} />
        </div>
      ))}

      {task.plan.length === 0 ? (
        <p className="text-sm text-slate-400">{task.status === TaskStatus.PLANNING ? 'Planning…' : 'No steps planned yet.'}</p>
      ) : (
        <ol className="space-y-3">
          {task.plan.map((step, i) => (
            <StepRow
              key={step.step_id}
              step={step}
              index={i}
              isCurrent={task.current_step === step.step_id}
              approval={approvalsByStep[step.step_id]}
              busy={busy}
              onDecide={onDecide}
            />
          ))}
        </ol>
      )}

      <details className="mt-4 rounded-lg ring-1 ring-slate-800">
        <summary className="cursor-pointer px-3 py-2 text-sm text-slate-300">Execution log ({events.length} events)</summary>
        <ul className="max-h-64 space-y-1 overflow-y-auto px-3 pb-3 text-xs">
          {events.length === 0 ? (
            <li className="text-slate-500">No events yet.</li>
          ) : (
            events.map((ev) => (
              <li key={ev.event_id} className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-1">
                <span className="font-mono text-slate-500">{formatTime(ev.timestamp)}</span>
                <StatusBadge status={ev.status} />
                <span className="text-slate-300">{ev.message}</span>
                {ev.step_id && <span className="font-mono text-slate-500">{ev.step_id}</span>}
                {ev.evidence_ids?.length > 0 && <span className="text-violet-300">{ev.evidence_ids.length} evidence</span>}
              </li>
            ))
          )}
        </ul>
      </details>
    </Card>
  )
}

function StepRow({ step, index, isCurrent, approval, busy, onDecide }) {
  const icon = STEP_ICON[step.status] ?? STEP_ICON.pending
  const verificationFailed = step.verification && step.verification.verified === false
  return (
    <li className={`flex gap-3 rounded-lg p-3 ring-1 ${isCurrent ? 'bg-slate-800/70 ring-violet-500/50' : 'ring-slate-800'}`}>
      <span className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full border-2 text-sm font-bold ${icon.cls}`}>{icon.icon}</span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium text-slate-100">
            {index + 1}. {step.description}
          </p>
          <StatusBadge status={step.status} />
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
          <code className="rounded bg-slate-950 px-1.5 py-0.5 text-violet-300">{step.tool_name}</code>
          {step.requires_approval && <span className="text-amber-300">🔒 needs approval</span>}
        </div>
        <p className="text-xs text-slate-400">Expected: {step.expected_outcome}</p>
        {step.error && <p className="text-xs text-rose-300">Error: {step.error}</p>}
        {verificationFailed && <p className="text-xs font-medium text-rose-300">Verification failed: outcome not confirmed</p>}
        {step.verification && (
          <details className="text-xs">
            <summary className="cursor-pointer text-slate-400">Verification details</summary>
            <JsonBlock value={step.verification} />
          </details>
        )}
        {approval && (
          <div className="pt-2">
            <ApprovalCard approval={approval} busy={busy} onDecide={onDecide} />
          </div>
        )}
      </div>
    </li>
  )
}