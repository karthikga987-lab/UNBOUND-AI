import { useEffect, useState } from 'react'
import { api } from '../api'
import { useTask } from '../hooks/useTask'
import ResultPanel from './ResultPanel'
import TimelinePanel from './TimelinePanel'
import { Card, ErrorState, LoadingState } from './ui'

export default function TaskWorkspace({ taskId, onTaskChange }) {
  const { task, events, error, refresh, setTask } = useTask(taskId)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)

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

  if (!task) {
    return (
      <Card title="Live execution">
        {error ? <ErrorState error={error} onRetry={refresh} title="Could not load task" /> : <LoadingState label="Loading task…" />}
      </Card>
    )
  }

  return (
    <div className="grid gap-4 2xl:grid-cols-5">
      <div className="min-w-0 2xl:col-span-3">
        <TimelinePanel
          task={task}
          events={events}
          error={error}
          busy={busy}
          actionError={actionError}
          onExecute={() => run('execute', () => api.executeTask(taskId))}
          onCancel={() => {
            if (window.confirm('Stop this task? Completed steps and evidence will be kept.')) run('cancel', () => api.cancelTask(taskId))
          }}
          onDecide={(approval, decision) =>
            run(`${decision === 'approved' ? 'approve' : 'reject'}:${approval.approval_id}`, () =>
              api.decideApproval(taskId, approval.approval_id, decision),
            )
          }
        />
      </div>
      <div className="min-w-0 2xl:col-span-2">
        <ResultPanel task={task} />
      </div>
    </div>
  )
}