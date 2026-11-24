import { useState } from 'react'
import { canCancel } from '../../api/contracts'
import { browserSessionService } from '../../live/browserSessionService'
import { deriveDisplayStatus } from '../../live/liveState'
import { useLiveSession } from '../../live/useLiveSession'
import ApprovalCard from '../ApprovalCard'
import { Button, Card } from '../ui'
import ActionOverlay from './ActionOverlay'
import ActivityTimeline from './ActivityTimeline'
import LiveStatusPanel from './LiveStatusPanel'
import ScreenshotViewer from './ScreenshotViewer'

function latestExecution(...entries) {
  return entries.filter(Boolean).sort((a, b) => a.time - b.time).at(-1)?.state ?? null
}

function latestVerifiedPage(task) {
  for (const step of [...(task?.plan ?? [])].reverse()) {
    const verification = step?.verification
    const observation = verification?.verified === true ? verification?.tool_result?.page_observation : null
    if (observation?.content_trust === 'untrusted_webpage_data') return observation
  }
  return null
}

// Live monitoring of a task's browser session. Execution, approval and safety stay in the backend:
// every control here calls a backend action and the UI only reflects what the backend reports.
export default function LiveView({ task, taskEvents, busy, actionError, onCancel, onDecide, onBack, service = browserSessionService }) {
  const { state: live, status, retry } = useLiveSession(task.task_id, service)
  const [control, setControl] = useState({ pending: null, error: null, confirmed: null })

  const execution = latestExecution(live.execution, control.confirmed)
  const cancelling = busy === 'cancel'
  const displayStatus = deriveDisplayStatus({ taskStatus: task.status, execution, cancelling })
  const settled = (control.pending === 'pause' && execution === 'paused') || (control.pending === 'resume' && execution === 'running')
  const pendingControl = settled ? null : control.pending

  async function runControl(kind) {
    setControl((c) => ({ ...c, pending: kind, error: null }))
    try {
      const result = await (kind === 'pause' ? service.pause(task.task_id) : service.resume(task.task_id))
      const state = typeof result?.execution_state === 'string' ? result.execution_state : null
      // Without an explicit state in the response, keep waiting for an execution_paused/resumed event.
      setControl((c) => ({ pending: state ? null : kind, error: null, confirmed: state ? { state, time: Date.now() } : c.confirmed }))
    } catch (err) {
      setControl((c) => ({ ...c, pending: null, error: err?.message ?? String(err) }))
    }
  }

  const warnings = []
  if (task.error) warnings.push({ level: 'error', text: `Task error: ${task.error}` })
  for (const step of task.plan) if (step.error) warnings.push({ level: 'error', text: `${step.description}: ${step.error}` })
  if (live.error) warnings.push({ level: 'error', text: `Browser: ${live.error}` })
  if (control.error) warnings.push({ level: 'error', text: control.error })
  if (actionError) warnings.push({ level: 'error', text: actionError.message ?? String(actionError) })
  if (cancelling) warnings.push({ level: 'warning', text: 'Stop requested. Waiting for the backend to confirm cancellation.' })
  if (pendingControl) warnings.push({ level: 'warning', text: `${pendingControl === 'pause' ? 'Pause' : 'Resume'} requested. Waiting for the backend to confirm.` })
  if (status === 'reconnecting') warnings.push({ level: 'warning', text: 'Live connection lost. Reconnecting…' })
  if (status === 'disconnected') warnings.push({ level: 'warning', text: 'Live connection lost. Use Reconnect on the screen to try again.' })
  if (task.status === 'needs_attention') warnings.push({ level: 'warning', text: 'The task needs attention before it can continue. Check the task details.' })

  const pauseEnabled = service.capabilities.pause && displayStatus === 'running' && !pendingControl
  const resumeEnabled = service.capabilities.resume && displayStatus === 'paused' && !pendingControl
  const stopEnabled = canCancel(task.status) && !cancelling
  const shot = live.screenshot
  const pageObservation = latestVerifiedPage(task)

  const controls = (
    <>
      <Button size="sm" variant="ghost" onClick={onBack}>
        ← Task details
      </Button>
      <Button
        size="sm"
        variant="secondary"
        disabled={!pauseEnabled}
        loading={pendingControl === 'pause'}
        onClick={() => runControl('pause')}
        title={service.capabilities.pause ? 'Pause execution' : 'Pause is not supported by the backend yet'}
      >
        ❚❚ Pause
      </Button>
      <Button
        size="sm"
        variant="secondary"
        disabled={!resumeEnabled}
        loading={pendingControl === 'resume'}
        onClick={() => runControl('resume')}
        title={service.capabilities.resume ? 'Resume execution' : 'Resume is not supported by the backend yet'}
      >
        ▶ Resume
      </Button>
      <Button size="sm" variant="danger" disabled={!stopEnabled} loading={cancelling} onClick={onCancel}>
        {cancelling ? 'Stopping…' : '■ Stop'}
      </Button>
    </>
  )

  return (
    <div className="space-y-4" data-testid="live-view">
      <div className="grid gap-4 2xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0 space-y-4">
          <Card title="Live View" subtitle="Verified browser observations and task progress. Screenshot streaming and direct browser controls are unavailable." actions={controls}>
            {pageObservation ? (
              <div className="space-y-2 rounded-lg bg-slate-950 p-4 ring-1 ring-slate-800" data-testid="task-backed-browser-observation">
                {isLocalDemoObservation(pageObservation) && <p className="text-[10px] font-semibold uppercase tracking-wide text-cyan-300">LOCAL DEMO MODE · fixed synthetic fixture only · public browsing disabled</p>}
                <p className="text-sm font-semibold text-slate-100">{pageObservation.title || 'Page title unavailable'}</p>
                <p className="break-all text-xs text-sky-300">{pageObservation.url}</p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-sm text-slate-300">{pageObservation.visible_text}</pre>
                <p className="text-[10px] uppercase tracking-wide text-amber-300">Verified task observation · untrusted page data · no screenshot stream</p>
              </div>
            ) : (
              <ScreenshotViewer screenshot={shot} status={status} onRetry={retry}>
                <ActionOverlay width={shot?.width} height={shot?.height} cursor={live.cursor} lastClick={live.lastClick} target={live.target} typing={live.typing} lastEventAt={live.lastEventAt} />
              </ScreenshotViewer>
            )}
            {!pageObservation && live.page?.url && <p className="mt-2 truncate text-xs text-slate-400" title={live.page.url}>🌐 {live.page.url}</p>}
          </Card>
          {task.pending_approvals.length > 0 && (
            <Card title="Review required" subtitle="UNBOUND has paused before a sensitive action. Nothing happens until you decide.">
              <div className="space-y-3" data-testid="live-approvals">
                {task.pending_approvals.map((approval) => (
                  <ApprovalCard key={approval.approval_id} approval={approval} busy={busy} onDecide={onDecide} />
                ))}
              </div>
            </Card>
          )}
        </div>
        <div className="min-w-0 space-y-4">
          <LiveStatusPanel task={task} taskEvents={taskEvents} live={live} displayStatus={displayStatus} connectionStatus={status} warnings={warnings} />
          <ActivityTimeline liveEvents={live.events} taskEvents={taskEvents} />
        </div>
      </div>
    </div>
  )
}

function isLocalDemoObservation(observation) {
  return typeof observation?.url === 'string' && /^http:\/\/unbound-demo\.invalid\/(?:product|details)\/?$/.test(observation.url)
}
