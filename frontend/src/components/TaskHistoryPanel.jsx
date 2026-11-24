import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Card, EmptyState, ErrorState, LoadingState, StatusBadge } from './ui'

export default function TaskHistoryPanel({ onSelect }) {
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
    let active = true
    api.listTasks(100).then((result) => {
      if (active) setTasks(Array.isArray(result) ? result : [])
    }).catch((err) => {
      if (active) setError(err)
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [])

  return (
    <Card title="Task history" subtitle="Saved task state is loaded from the backend.">
      {loading ? <LoadingState label="Loading task history…" /> : null}
      {!loading && error ? <ErrorState error={error} onRetry={load} title="Task history unavailable" /> : null}
      {!loading && !error && tasks.length === 0 ? (
        <EmptyState title="No saved tasks" hint="Prepared and completed tasks will appear here." />
      ) : null}
      {!loading && !error && tasks.length > 0 ? (
        <ul className="space-y-2">
          {[...tasks].reverse().map((task) => (
            <li key={task.task_id}>
              <button
                type="button"
                onClick={() => onSelect(task.task_id)}
                className="w-full rounded-lg border border-slate-800 bg-slate-950/70 p-3 text-left hover:border-violet-500/50"
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
