import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { isTerminal, TaskStatus } from '../api/contracts'
import { describePlan, describeResult, describeStatus, narrateChange, parseCommand } from '../voice/narration'
import { useVoiceMode } from '../voice/useVoiceMode'
import { Button, Card, ErrorState } from './ui'
import VoiceBubble from './VoiceBubble'

const EXAMPLES = [
  'How does UNBOUND verify a task result?',
  'What is a good way to organize a project plan?',
  'Read C:\\path\\to\\report.pdf and summarize the key findings',
]

export default function ChatPanel({
  history,
  activeTaskId,
  activeTask,
  onSelect,
  onSubmit,
  onNewConversation,
  loading = false,
  conversationError = null,
  unavailableTaskCount = 0,
}) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const listRef = useRef(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [history.length])

  const send = useCallback(async (value) => {
    setSending(true)
    setError(null)
    try {
      return await onSubmit(value)
    } catch (err) {
      setError(err)
      throw err
    } finally {
      setSending(false)
    }
  }, [onSubmit])

  async function submit(event) {
    event?.preventDefault()
    const value = text.trim()
    if (!value || sending || loading || !onSubmit) return
    try {
      await send(value)
      setText('')
    } catch {
      /* shown via error state; keep the text available for retry */
    }
  }

  const activeTaskRef = useRef(activeTask)
  useEffect(() => {
    activeTaskRef.current = activeTask
  }, [activeTask])

  // Short voice commands can explicitly operate on the selected task. All new
  // utterances use the same backend intent-classification path as typed chat.
  const handleSpoken = async (spoken) => {
    const task = activeTaskRef.current
    switch (parseCommand(spoken)) {
      case 'run':
        if (!task) return 'There is no task to run yet. Tell me what you need.'
        if (task.status !== TaskStatus.CREATED) return describeStatus(task)
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
        const response = await send(spoken)
        return response?.task ? describePlan(response.task, false) : response?.reply ?? 'I could not prepare a response.'
      }
    }
  }
  const voice = useVoiceMode({ onUtterance: handleSpoken })

  const { active: voiceActive, announce } = voice
  const seenRef = useRef(null)
  useEffect(() => {
    if (!activeTask) return
    const prev = seenRef.current
    seenRef.current = { task_id: activeTask.task_id, status: activeTask.status, current_step: activeTask.current_step }
    if (!voiceActive || prev?.task_id !== activeTask.task_id) return
    const line = narrateChange(prev, activeTask)
    if (line) announce(line.text, { replaceable: line.replaceable })
  }, [activeTask, voiceActive, announce])

  return (
    <Card
      title="Talk with UNBOUND AI"
      subtitle="Chat, ask a question, or request an action. UNBOUND classifies each message; task plans remain separate from approval and execution."
      actions={onNewConversation && <Button size="sm" variant="ghost" disabled={loading || sending} onClick={onNewConversation}>New conversation</Button>}
    >
      <div ref={listRef} className="max-h-80 space-y-3 overflow-y-auto pr-1" aria-live="polite">
        {unavailableTaskCount > 0 && (
          <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
            {unavailableTaskCount} earlier task {unavailableTaskCount === 1 ? 'result is' : 'results are'} unavailable because task state is held in memory and the backend restarted. No file was reopened. Submit a new approved request only if you want a fresh analysis.
          </p>
        )}
        {loading && <p className="py-3 text-sm text-slate-400">Restoring conversation…</p>}
        {history.map((item) => (
          <div key={item.message_id ?? item.id} className={`space-y-1 ${item.role === 'user' ? 'text-right' : 'text-left'}`}>
            <p className={`inline-block max-w-[90%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${item.role === 'user' ? 'rounded-br-sm bg-violet-600 text-white' : 'rounded-bl-sm border border-slate-800 bg-slate-900 text-slate-200'}`}>
              {item.content}
            </p>
            {item.task_id && (
              <div className="text-left">
                <button
                  type="button"
                  onClick={() => onSelect(item.task_id)}
                  className={`rounded-full border px-3 py-1 text-xs ${item.task_id === activeTaskId ? 'border-violet-500/60 bg-slate-800 text-violet-200' : 'border-slate-700 text-slate-300 hover:border-violet-500'}`}
                >
                  {item.task_id === activeTaskId ? 'Showing prepared task below' : 'Open prepared task'}
                  {item.task_status ? ` · ${item.task_status.replaceAll('_', ' ')}` : ''}
                </button>
              </div>
            )}
          </div>
        ))}
        {history.length === 0 && !loading && (
          <div className="space-y-2">
            <p className="text-sm text-slate-400">Send a message to get started:</p>
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
          placeholder="Ask a question, have a conversation, or describe an action to plan"
          disabled={loading || !onSubmit}
          className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-violet-500 focus:outline-none"
        />
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-slate-500">Task plans are never run from a chat response. Review the plan and use Run plan separately.</p>
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
            <Button type="submit" loading={sending} disabled={!text.trim() || loading || !onSubmit}>
              {sending ? 'Thinking…' : 'Send'}
            </Button>
          </div>
        </div>
        {conversationError && <ErrorState error={conversationError} compact title="Conversation unavailable" />}
        {error && <ErrorState error={error} compact title="Could not send message" />}
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
