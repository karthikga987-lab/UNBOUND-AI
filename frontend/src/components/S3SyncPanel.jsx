import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Button, Card, EmptyState } from './ui'

export default function S3SyncPanel() {
  const [status, setStatus] = useState(null)
  const [backups, setBackups] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextBackups] = await Promise.all([api.s3SyncStatus(), api.listS3Backups()])
      setStatus(nextStatus)
      setBackups(nextBackups)
      setError('')
    } catch (err) {
      setError(err?.message || 'S3 status could not be loaded.')
    }
  }, [])

  useEffect(() => {
    let active = true
    Promise.all([api.s3SyncStatus(), api.listS3Backups()]).then(([nextStatus, nextBackups]) => {
      if (!active) return
      setStatus(nextStatus)
      setBackups(nextBackups)
    }).catch((err) => {
      if (active) setError(err?.message || 'S3 status could not be loaded.')
    })
    return () => { active = false }
  }, [])

  async function sync() {
    if (!window.confirm('Upload a consistent snapshot of task history, evidence, and indexed documents to the configured private S3 bucket?')) return
    setBusy(true)
    try {
      setStatus(await api.syncToS3())
      await refresh()
    } catch (err) {
      setError(err?.message || 'S3 synchronization failed.')
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  async function restore(key) {
    if (!window.confirm(`Restore ${key} to a new recovery file? The active database will not be replaced.`)) return
    setBusy(true)
    try {
      const result = await api.restoreTaskBackup(key)
      window.alert(`Verified recovery copy created at ${result.destination}`)
    } catch (err) {
      setError(err?.message || 'Backup restore failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Private S3 backup and sync">
      <div className="space-y-3 text-sm">
        <p className="text-slate-300">Uploads task history, evidence and supported indexed documents as a consistent snapshot. No remote objects are deleted.</p>
        {error && <p role="alert" className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-rose-200">{error}</p>}
        {!status ? <EmptyState title="Loading sync status" /> : (
          <div className="rounded border border-slate-700 bg-slate-950 p-3">
            <p>Status: <strong>{status.status}</strong></p>
            <p>Configuration: {status.enabled ? `${status.bucket} · ${status.region}` : 'Automatic sync disabled'}</p>
            {status.last_success_at && <p>Last successful sync: {new Date(status.last_success_at).toLocaleString()}</p>}
            <p>Pending: {status.pending_count ?? 0} · Retry count: {status.retry_count ?? 0}</p>
            {status.last_error_code && <p role="status">Last failure category: {status.last_error_code}</p>}
            {status.next_retry_at && <p>Next retry: {new Date(status.next_retry_at).toLocaleString()}</p>}
          </div>
        )}
        <div className="flex gap-2">
          <Button disabled={busy || status?.enabled !== true} onClick={sync}>{busy ? 'Working…' : 'Sync now'}</Button>
          <Button disabled={busy} onClick={refresh}>Refresh</Button>
        </div>
        {!status?.enabled && <p className="text-amber-300">Enable with UNBOUND_S3_SYNC_ENABLED=1 after reviewing the data-transfer scope. AWS credentials remain backend-only.</p>}
        <h3 className="font-medium text-slate-200">Available recovery points</h3>
        {backups.length ? backups.map((item) => (
          <div key={item.key} className="flex items-center justify-between gap-2 rounded border border-slate-800 p-2">
            <span className="break-all text-xs">{item.kind === 'application_snapshot' ? 'Application snapshot' : 'Task database backup'} · {item.key} · {item.size_bytes} bytes</span>
            <Button disabled={busy} onClick={() => restore(item.key)}>Restore to new location</Button>
          </div>
        )) : <p className="text-slate-500">No S3 recovery points listed.</p>}
      </div>
    </Card>
  )
}
