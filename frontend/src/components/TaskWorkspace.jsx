import { useEffect, useState } from 'react'
import { api } from '../api'
import { isTerminal } from '../api/contracts'
import { useTask } from '../hooks/useTask'
import LiveView from './live/LiveView'
import ResultPanel from './ResultPanel'
import TimelinePanel from './TimelinePanel'
import { Card, ErrorState, LoadingState } from './ui'

const VIEWS = [
  { id: 'details', label: 'Task details' },
  { id: 'live', label: 'Live View' },
]

export default function TaskWorkspace({ taskId, onTaskChange }) {
  const { task, events, error, refresh, setTask } = useTask(taskId)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [view, setView] = useState('details')

  useEffect(() => {
    if (task) onTaskChange(task)
  }, [task, onTaskChange])

  async function run(key, action) {
    setBusy(key)
    setActionError(null)
    try {
      const updated = await action()
      if (updated?.task_id) setTask(updated)
      await refresh()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(null)
    }
  }

  const execute = () => run('execute', () => api.executeTask(taskId))

  const cancel = () => {
    if (window.confirm('Stop this task? Completed steps and evidence will be kept. An action already in progress may not be reversible.')) {
      run('cancel', () => api.cancelTask(taskId))
    }
  }

  // The backend records the decision only; it does not resume execution by itself
  // (docs/API_CONTRACT.md), so once no approvals are pending we ask it to continue.
  const decide = (approval, decision) =>
    run(`${decision === 'approved' ? 'approve' : 'reject'}:${approval.approval_id}`, async () => {
      const updated = await api.decideApproval(taskId, approval.approval_id, decision)
      if (updated && !isTerminal(updated.status) && (updated.pending_approvals?.length ?? 0) === 0) {
        return api.executeTask(taskId)
      }
      return updated
    })

  if (!task) {
    return (
      <Card title="Live execution">
        {error ? <ErrorState error={error} onRetry={refresh} title="Could not load task" /> : <LoadingState label="Loading task…" />}
      </Card>
    )
  }

  return (
    <div className="space-y-3">
      <div className="inline-flex rounded-lg bg-slate-900 p-1 ring-1 ring-slate-800" role="tablist">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            type="button"
            role="tab"
            aria-selected={view === v.id}
            onClick={() => setView(v.id)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${view === v.id ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
          >
            {v.id === 'live' && <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-rose-500 align-middle" />}
            {v.label}
          </button>
        ))}
      </div>

      {view === 'live' ? (
        <LiveView task={task} taskEvents={events} busy={busy} actionError={actionError} onCancel={cancel} onDecide={decide} onBack={() => setView('details')} />
      ) : (
        <div className="grid gap-4 2xl:grid-cols-5">
          <div className="min-w-0 2xl:col-span-3">
            <TimelinePanel
              task={task}
              events={events}
              error={error}
              busy={busy}
              actionError={actionError}
              onExecute={execute}
              onCancel={cancel}
              onDecide={decide}
              onOpenLive={() => setView('live')}
            />
          </div>
          <div className="min-w-0 2xl:col-span-2">
            <ResultPanel task={task} />
          </div>
        </div>
      )}
    </div>
  )
}