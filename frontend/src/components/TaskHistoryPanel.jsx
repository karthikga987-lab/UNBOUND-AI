import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Card, EmptyState, ErrorState, LoadingState, StatusBadge } from './ui'

export default function TaskHistoryPanel({ onSelect, active = false, compact = false }) {
  const [tasks, setTasks] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const result = await api.listTasks(100)
      setTasks(Array.isArray(result) ? result : [])
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let isCurrent = true
    api.listTasks(100).then((result) => {
      if (isCurrent) setTasks(Array.isArray(result) ? result : [])
    }).catch((err) => {
      if (isCurrent) setError(err)
    }).finally(() => {
      if (isCurrent) setLoading(false)
    })
    return () => { isCurrent = false }
  }, [active])

  const visibleTasks = compact ? [...tasks].reverse().slice(0, 3) : [...tasks].reverse()

  return (
    <Card title={compact ? null : 'Task history'} subtitle={compact ? undefined : 'Saved task state is loaded from the backend.'} className={compact ? 'compact-history-list' : ''}>
      {loading ? <LoadingState label="Loading task history…" /> : null}
      {!loading && error ? <ErrorState error={error} onRetry={load} title="Task history unavailable" /> : null}
      {!loading && !error && tasks.length === 0 ? (
        <EmptyState title="No saved tasks" hint="Prepared and completed tasks will appear here." />
      ) : null}
      {!loading && !error && tasks.length > 0 ? (
        <ul className="space-y-2">
          {visibleTasks.map((task) => (
            <li key={task.task_id}>
              <button
                type="button"
                onClick={() => onSelect(task.task_id)}
                className={`w-full rounded-lg border border-slate-800 bg-slate-950/70 p-3 text-left hover:border-violet-500/50 ${compact ? 'recent-task-row' : ''}`}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="line-clamp-2 text-sm text-slate-200">{task.user_request}</span>
                  <StatusBadge status={task.status} />
                </span>
                <span className="mt-1 block text-xs text-slate-500">
                  Updated {task.updated_at ? new Date(task.updated_at).toLocaleString() : 'time unavailable'}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  )
}
