import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { isTerminal, TaskStatus } from '../api/contracts'
import { describePlan, describeResult, describeStatus, narrateChange, parseCommand } from '../voice/narration'
import { useVoiceMode } from '../voice/useVoiceMode'
import { Button, Card, ErrorState, StatusBadge } from './ui'
import VoiceBubble from './VoiceBubble'

const EXAMPLES = [
  'Get the latest report from the reports website and compare it with my uploaded documents',
  'Find every mention of Q3 revenue in my documents and summarise the differences',
  'Replay my "Download latest report" workflow and create a summary report',
]

export default function ChatPanel({ history, activeTaskId, activeTask, onSelect, onSubmit }) {
  const [text, setText] = useState('')
  const [autoExecute, setAutoExecute] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const listRef = useRef(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length])

  // Shared by the text form and voice mode so both take the same path.
  const send = useCallback(
    async (value) => {
      setSending(true)
      setError(null)
      try {
        return await onSubmit(value, autoExecute)
      } catch (err) {
        setError(err)
        throw err
      } finally {
        setSending(false)
      }
    },
    [onSubmit, autoExecute],
  )

  async function submit(event) {
    event?.preventDefault()
    const value = text.trim()
    if (!value || sending) return
    try {
      await send(value)
      setText('')
    } catch {
      /* shown via error state */
    }
  }

  const activeTaskRef = useRef(activeTask)
  useEffect(() => {
    activeTaskRef.current = activeTask
  }, [activeTask])

  // Voice: short commands act on the current task; anything else becomes a new task.
  // Every reply is built from the real task returned by the API.
  const handleSpoken = async (spoken) => {
    const task = activeTaskRef.current
    switch (parseCommand(spoken)) {
      case 'run':
        if (!task) return 'There is no task to run yet. Tell me what you need.'
        if (task.status !== TaskStatus.CREATED && task.status !== TaskStatus.NEEDS_ATTENTION) return describeStatus(task)
        await api.executeTask(task.task_id)
        return 'Running it now.'
      case 'cancel':
        if (!task || isTerminal(task.status)) return 'There is no running task to cancel.'
        await api.cancelTask(task.task_id)
        return 'Stopping the task.'
      case 'approve':
        return 'For safety, approvals need a click. Use Approve or Reject on screen.'
      case 'results':
        return task?.status === TaskStatus.COMPLETED ? describeResult(task) : describeStatus(task)
      case 'status':
        return describeStatus(task)
      case 'stop':
        voice.stop()
        return ''
      default: {
        const created = await send(spoken)
        return created ? describePlan(created, autoExecute) : 'Done.'
      }
    }
  }
  const voice = useVoiceMode({ onUtterance: handleSpoken })

  // Narrate real progress of the active task while voice mode is on.
  const { active: voiceActive, announce } = voice
  const seenRef = useRef(null)
  useEffect(() => {
    if (!activeTask) return
    const prev = seenRef.current
    seenRef.current = { task_id: activeTask.task_id, status: activeTask.status, current_step: activeTask.current_step }
    // A newly selected task is only a baseline: its plan is described by the reply to the request.
    if (!voiceActive || prev?.task_id !== activeTask.task_id) return
    const line = narrateChange(prev, activeTask)
    if (line) announce(line.text, { replaceable: line.replaceable })
  }, [activeTask, voiceActive, announce])

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
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={voice.active ? voice.stop : voice.start}
              aria-pressed={voice.active}
              aria-label={voice.active ? 'End voice mode' : 'Start voice mode'}
              title={voice.active ? 'End voice mode' : 'Talk to UNBOUND AI'}
              className={`grid h-9 w-9 place-items-center rounded-lg border transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-400 ${
                voice.active ? 'border-violet-400 bg-violet-500/20 text-violet-200' : 'border-slate-700 text-slate-300 hover:border-violet-500 hover:text-white'
              }`}
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="9" y="3" width="6" height="11" rx="3" />
                <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
              </svg>
            </button>
            <Button type="submit" loading={sending} disabled={!text.trim()}>
              {sending ? 'Planning…' : 'Send'}
            </Button>
          </div>
        </div>
        {error && <ErrorState error={error} compact title="Could not create task" />}
      </form>
      {voice.active && (
        <VoiceBubble
          status={voice.status}
          transcript={voice.transcript}
          message={voice.message}
          audioRef={voice.audioRef}
          onTap={voice.tap}
          onToggleMute={voice.toggleMute}
          onEnd={voice.stop}
        />
      )}
    </Card>
  )
}