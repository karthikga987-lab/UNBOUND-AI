import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { asList, formatDateTime } from '../lib/format'
import { Button, Card, EmptyState, ErrorState, Input, LoadingState } from './ui'

export default function WorkflowPanel({ onTaskStarted }) {
  const [workflows, setWorkflows] = useState([])
  const [loadState, setLoadState] = useState({ loading: true, error: null })
  const [form, setForm] = useState({ name: '', startUrl: '' })
  const [recording, setRecording] = useState(null)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [replayTarget, setReplayTarget] = useState(null)
  const [params, setParams] = useState({})

  const load = useCallback(async () => {
    try {
      setWorkflows(asList(await api.listWorkflows(), 'workflows'))
      setLoadState({ loading: false, error: null })
    } catch (error) {
      setLoadState({ loading: false, error })
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const reload = () => {
    setLoadState({ loading: true, error: null })
    load()
  }

  async function act(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(null)
    }
  }

  function startRecording(event) {
    event.preventDefault()
    const name = form.name.trim()
    const startUrl = form.startUrl.trim()
    act('start', async () => {
      const res = await api.startRecording({ name, startUrl })
      setRecording({ name, start_url: startUrl, ...(res && typeof res === 'object' ? res : {}) })
    })
  }

  function stopRecording() {
    act('stop', async () => {
      const wf = await api.stopRecording()
      setRecording(null)
      setForm({ name: '', startUrl: '' })
      if (wf?.workflow_id) setWorkflows((prev) => [wf, ...prev.filter((w) => w.workflow_id !== wf.workflow_id)])
      else await load()
    })
  }

  function replay(wf, values) {
    act(`replay:${wf.workflow_id}`, async () => {
      const missing = (wf.parameters ?? []).filter((p) => p.required && String(values[p.name] ?? '').trim() === '')
      if (missing.length) throw new Error(`Please fill in: ${missing.map((p) => p.name).join(', ')}`)
      const task = await api.replayWorkflow(wf.workflow_id, values)
      setReplayTarget(null)
      if (task?.task_id) onTaskStarted(task)
    })
  }

  function openReplay(wf) {
    if (!wf.parameters?.length) {
      replay(wf, {})
      return
    }
    setReplayTarget(wf.workflow_id)
    setParams(Object.fromEntries(wf.parameters.map((p) => [p.name, p.default ?? ''])))
  }

  return (
    <Card title="Workflow learning" subtitle="Teach the agent once by demonstration, then replay it with new parameters.">
      <div className="space-y-4">
        {recording ? (
          <div className="rounded-lg border border-rose-500/50 bg-rose-500/10 p-3">
            <p className="flex items-center gap-2 text-sm font-semibold text-rose-200">
              <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-rose-500" /> Recording “{recording.name || 'Untitled'}”
            </p>
            <p className="mt-1 break-all text-xs text-slate-300">
              Perform the steps in the browser window UNBOUND AI opened at {recording.start_url}. Click stop when you are done.
            </p>
            <Button className="mt-3 w-full" variant="danger" loading={busy === 'stop'} onClick={stopRecording}>
              ■ Stop & save workflow
            </Button>
          </div>
        ) : (
          <form onSubmit={startRecording} className="space-y-2">
            <Input required placeholder="Workflow name (e.g. Download monthly invoice)" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
            <Input required type="url" placeholder="Start URL (https://…)" value={form.startUrl} onChange={(e) => setForm((f) => ({ ...f, startUrl: e.target.value }))} />
            <Button type="submit" className="w-full" loading={busy === 'start'}>
              ● Start recording
            </Button>
          </form>
        )}
        {actionError && <ErrorState error={actionError} compact title="Workflow action failed" />}

        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Saved workflows</h3>
            <Button size="sm" variant="ghost" onClick={reload}>
              Refresh
            </Button>
          </div>
          {loadState.loading ? (
            <LoadingState label="Loading workflows…" />
          ) : loadState.error ? (
            <ErrorState error={loadState.error} onRetry={reload} title="Could not load workflows" />
          ) : workflows.length === 0 ? (
            <EmptyState title="No workflows yet" hint="Record a demonstration above to create one." />
          ) : (
            <ul className="space-y-2">
              {workflows.map((wf) => (
                <li key={wf.workflow_id} className="rounded-lg p-3 ring-1 ring-slate-800">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-slate-100">{wf.name}</p>
                      <p className="truncate text-xs text-slate-500">{wf.start_url}</p>
                      <p className="mt-1 text-xs text-slate-400">
                        {wf.steps?.length ?? 0} steps · {wf.parameters?.length ?? 0} params · {formatDateTime(wf.updated_at ?? wf.created_at)}
                      </p>
                    </div>
                    <Button size="sm" variant="secondary" loading={busy === `replay:${wf.workflow_id}`} onClick={() => openReplay(wf)}>
                      ▶ Replay
                    </Button>
                  </div>
                  {wf.description && <p className="mt-1 text-xs text-slate-400">{wf.description}</p>}
                  {replayTarget === wf.workflow_id && (
                    <form
                      className="mt-3 space-y-2 border-t border-slate-800 pt-3"
                      onSubmit={(e) => {
                        e.preventDefault()
                        replay(wf, params)
                      }}
                    >
                      {wf.parameters.map((p) => (
                        <label key={p.name} className="block text-xs text-slate-400">
                          {p.name}
                          {p.required && <span className="text-rose-400"> *</span>}
                          {p.description && <span className="text-slate-500">: {p.description}</span>}
                          <Input className="mt-1" value={params[p.name] ?? ''} onChange={(e) => setParams((v) => ({ ...v, [p.name]: e.target.value }))} />
                        </label>
                      ))}
                      <div className="flex gap-2">
                        <Button type="submit" size="sm" loading={busy === `replay:${wf.workflow_id}`}>
                          Run replay
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setReplayTarget(null)}>
                          Cancel
                        </Button>
                      </div>
                    </form>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  )
}