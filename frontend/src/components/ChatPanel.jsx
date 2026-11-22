import { useEffect, useRef, useState } from 'react'
import { Button, Card, ErrorState, StatusBadge } from './ui'

const EXAMPLES = [
  'Get the latest report from the reports website and compare it with my uploaded documents',
  'Find every mention of Q3 revenue in my documents and summarise the differences',
  'Replay my "Download latest report" workflow and create a summary report',
]

export default function ChatPanel({ history, activeTaskId, onSelect, onSubmit }) {
  const [text, setText] = useState('')
  const [autoExecute, setAutoExecute] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const listRef = useRef(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length])

  async function submit(event) {
    event?.preventDefault()
    const value = text.trim()
    if (!value || sending) return
    setSending(true)
    setError(null)
    try {
      await onSubmit(value, autoExecute)
      setText('')
    } catch (err) {
      setError(err)
    } finally {
      setSending(false)
    }
  }

  return (
    <Card
      title="Ask UNBOUND AI"
      subtitle="Describe a goal in plain language. The agent plans with registered tools, asks before sensitive actions, and verifies results."
    >
      <div ref={listRef} className="max-h-64 space-y-3 overflow-y-auto pr-1">
        {history.length === 0 ? (
          <div className="space-y-2">
            <p className="text-sm text-slate-400">Try one of these:</p>
            <div className="flex flex-wrap gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => setText(example)}
                  className="rounded-full border border-slate-700 px-3 py-1.5 text-left text-xs text-slate-300 hover:border-violet-500 hover:text-white"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : (
          history.map((item) => (
            <div key={item.task_id} className="space-y-1.5">
              <div className="flex justify-end">
                <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-violet-600 px-3 py-2 text-sm text-white">{item.user_request}</p>
              </div>
              <button
                type="button"
                onClick={() => onSelect(item.task_id)}
                className={`flex w-full max-w-[85%] items-center justify-between gap-3 rounded-2xl rounded-bl-sm border px-3 py-2 text-left text-sm transition ${
                  item.task_id === activeTaskId ? 'border-violet-500/60 bg-slate-800' : 'border-slate-800 bg-slate-900 hover:border-slate-600'
                }`}
              >
                <span className="text-slate-300">{item.task_id === activeTaskId ? 'Showing this task below' : 'View this task'}</span>
                <StatusBadge status={item.status} />
              </button>
            </div>
          ))
        )}
      </div>

      <form onSubmit={submit} className="mt-4 space-y-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) submit(e)
          }}
          rows={3}
          placeholder="e.g. Retrieve the latest report from the website, compare it with my PDF, and give me a cited summary"
          className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-violet-500 focus:outline-none"
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input type="checkbox" checked={autoExecute} onChange={(e) => setAutoExecute(e.target.checked)} className="accent-violet-500" />
            Run automatically after planning <span className="text-slate-500">(sensitive steps still need approval)</span>
          </label>
          <Button type="submit" loading={sending} disabled={!text.trim()}>
            {sending ? 'Planning…' : 'Send'}
          </Button>
        </div>
        {error && <ErrorState error={error} compact title="Could not create task" />}
      </form>
    </Card>
  )
}