import { useCallback, useEffect, useState } from 'react'
import { api, USE_MOCK } from './api'
import { TaskStatus } from './api/contracts'
import ChatPanel from './components/ChatPanel'
import DataPanel from './components/DataPanel'
import TaskWorkspace from './components/TaskWorkspace'
import WorkflowPanel from './components/WorkflowPanel'
import { Card, EmptyState, StatusBadge } from './components/ui'

const TABS = [
  { id: 'workflows', label: 'Workflows' },
  { id: 'data', label: 'Connected data' },
]

export default function App() {
  const [history, setHistory] = useState([])
  const [activeTaskId, setActiveTaskId] = useState(null)
  const [activeTask, setActiveTask] = useState(null) // latest polled copy, used for voice narration
  const [tab, setTab] = useState('workflows')
  const [health, setHealth] = useState('checking')

  useEffect(() => {
    let alive = true
    api.health().then(
      () => alive && setHealth('online'),
      () => alive && setHealth('offline'),
    )
    return () => {
      alive = false
    }
  }, [])

  const trackTask = useCallback((task) => {
    setHistory((prev) => [
      ...prev.filter((item) => item.task_id !== task.task_id),
      { task_id: task.task_id, user_request: task.user_request, status: task.status },
    ])
    setActiveTaskId(task.task_id)
    setActiveTask(task)
  }, [])

  const handleSubmit = useCallback(
    async (text, autoExecute) => {
      const task = await api.createTask(text, autoExecute)
      trackTask(task)
      if (autoExecute && task.status === TaskStatus.CREATED) await api.executeTask(task.task_id)
      return task
    },
    [trackTask],
  )

  const handleTaskChange = useCallback((task) => {
    setActiveTask(task)
    setHistory((prev) => {
      const item = prev.find((h) => h.task_id === task.task_id)
      if (!item || item.status === task.status) return prev
      return prev.map((h) => (h.task_id === task.task_id ? { ...h, status: task.status } : h))
    })
  }, [])

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-linear-to-br from-violet-500 to-cyan-400 font-black text-slate-950">U</div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">
                UNBOUND <span className="text-violet-400">AI</span>
              </h1>
              <p className="hidden text-xs text-slate-400 sm:block">Learns workflows · connects information · verifies outcomes</p>
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs">
            {USE_MOCK && <span className="rounded-full border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-amber-300">Mock data</span>}
            <span className="hidden text-slate-400 sm:inline">Backend</span>
            <StatusBadge status={health} />
          </div>
        </div>
      </header>

      {health === 'offline' && !USE_MOCK && (
        <div className="mx-auto max-w-[1600px] px-4 pt-4">
          <p className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
            The backend is not reachable. Start it, or set VITE_USE_MOCK=true in frontend/.env.local to use mock data.
          </p>
        </div>
      )}

      <main className="mx-auto grid max-w-[1600px] gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-4">
          <ChatPanel
            history={history}
            activeTaskId={activeTaskId}
            activeTask={activeTask?.task_id === activeTaskId ? activeTask : null}
            onSelect={setActiveTaskId}
            onSubmit={handleSubmit}
          />
          {activeTaskId ? (
            <TaskWorkspace key={activeTaskId} taskId={activeTaskId} onTaskChange={handleTaskChange} />
          ) : (
            <Card title="Live execution">
              <EmptyState title="No task running" hint="Send a request above or replay a saved workflow to see the plan, live steps, approvals and results here." />
            </Card>
          )}
        </div>

        <aside className="min-w-0 space-y-3">
          <div className="flex rounded-lg bg-slate-900 p-1 ring-1 ring-slate-800">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ${tab === t.id ? 'bg-slate-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {/* Both stay mounted so an in-progress recording is not lost when switching tabs. */}
          <div className={tab === 'workflows' ? '' : 'hidden'}>
            <WorkflowPanel onTaskStarted={trackTask} />
          </div>
          <div className={tab === 'data' ? '' : 'hidden'}>
            <DataPanel />
          </div>
        </aside>
      </main>
    </div>
  )
}