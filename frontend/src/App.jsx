import { useCallback, useEffect, useState } from 'react'
import { api, MOCK_AREAS, USE_MOCK } from './api'
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
  const [messages, setMessages] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [conversationLoading, setConversationLoading] = useState(true)
  const [conversationError, setConversationError] = useState(null)
  const [activeTaskId, setActiveTaskId] = useState(null)
  const [activeTask, setActiveTask] = useState(null) // latest polled copy, used for voice narration
  const [unavailableTaskCount, setUnavailableTaskCount] = useState(0)
  const [tab, setTab] = useState('workflows')
  const [health, setHealth] = useState('checking')

  useEffect(() => {
    let alive = true
    api.health().then(() => alive && setHealth('online'), () => alive && setHealth('offline'))
    const restoreConversation = async () => {
      try {
        const savedId = window.localStorage.getItem('unbound.conversation_id')
        if (savedId) {
          try {
            const conversation = await api.getConversation(savedId)
            if (!alive) return
            setConversationId(conversation.session_id)
            setMessages(conversation.messages)
            const taskReferences = await api.getConversationTaskReferences(savedId)
            if (!alive) return
            const latestTaskId = taskReferences.available_task_ids?.at(-1)
            if (latestTaskId) setActiveTaskId(latestTaskId)
            setUnavailableTaskCount(taskReferences.unavailable_task_ids?.length ?? 0)
            setConversationLoading(false)
            return
          } catch (err) {
            if (err?.status !== 404) throw err
            window.localStorage.removeItem('unbound.conversation_id')
          }
        }
        const conversation = await api.createConversation()
        if (!alive) return
        window.localStorage.setItem('unbound.conversation_id', conversation.session_id)
        setConversationId(conversation.session_id)
        setMessages([])
        setConversationLoading(false)
      } catch (err) {
        if (!alive) return
        setConversationError(err)
        setConversationLoading(false)
      }
    }
    restoreConversation()
    return () => {
      alive = false
    }
  }, [])

  const handleSubmit = useCallback(async (text) => {
    if (!conversationId) throw new Error('Conversation is still being restored. Please try again.')
    const intent = await api.classifyAssistantRequest(text, conversationId)
    if (['conversation', 'information', 'drafting'].includes(intent.category)) {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
      const exchange = await api.sendConversationMessage(conversationId, text, timezone)
      setMessages((prev) => [...prev, exchange.user_message, exchange.assistant_message])
      setConversationError(null)
      return { intent, reply: exchange.reply, task: null }
    }

    // Clarifications and actionable turns use the assistant route, which
    // validates again before preparing a task and never executes it.
    const response = await api.assistantRequest(text, conversationId)
    const messageKey = `${Date.now()}-${Math.random().toString(16).slice(2)}`
    setMessages((prev) => [...prev,
      { id: `${messageKey}-user`, role: 'user', content: text },
      {
        id: `${messageKey}-assistant`,
        role: 'assistant',
        content: response.reply,
        task_id: response.task?.task_id,
        task_status: response.task?.status,
      },
    ])
    if (response.task) {
      setActiveTaskId(response.task.task_id)
      setActiveTask(response.task)
    }
    setConversationError(null)
    return response
  }, [conversationId])

  const startNewConversation = useCallback(async () => {
    setConversationLoading(true)
    setConversationError(null)
    try {
      const conversation = await api.createConversation()
      window.localStorage.setItem('unbound.conversation_id', conversation.session_id)
      setConversationId(conversation.session_id)
      setMessages([])
      setActiveTaskId(null)
      setActiveTask(null)
      setUnavailableTaskCount(0)
    } catch (err) {
      setConversationError(err)
    } finally {
      setConversationLoading(false)
    }
  }, [])

  const trackTask = useCallback((task) => {
    setActiveTaskId(task.task_id)
    setActiveTask(task)
  }, [])

  const handleTaskChange = useCallback((task) => {
    setActiveTask(task)
    setMessages((prev) => prev.map((message) => message.task_id === task.task_id
      ? { ...message, task_status: task.status }
      : message))
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
            {(USE_MOCK || MOCK_AREAS.size > 0) && (
              <span
                className="rounded-full border border-amber-500/30 bg-amber-500/15 px-2 py-0.5 text-amber-300"
                title="Mock responses are simulated and do not perform real actions."
              >
                {USE_MOCK ? 'Mock data: all areas' : `Mock data: ${[...MOCK_AREAS].join(', ')}`}
              </span>
            )}
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
            history={messages}
            activeTaskId={activeTaskId}
            activeTask={activeTask?.task_id === activeTaskId ? activeTask : null}
            onSelect={setActiveTaskId}
            onSubmit={handleSubmit}
            onNewConversation={startNewConversation}
            loading={conversationLoading}
            conversationError={conversationError}
            unavailableTaskCount={unavailableTaskCount}
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
