import { useCallback, useEffect, useRef, useState } from 'react'
import { api, isMocked } from '../api'
import { asList, formatBytes, formatDateTime } from '../lib/format'
import { Button, Card, EmptyState, ErrorState, Input, LoadingState, StatusBadge } from './ui'

const ACCEPT = '.pdf,.docx,.txt'
const fetchDocuments = async () => asList(await api.listDocuments(), 'documents')
const fetchIntegrations = async () => asList(await api.listIntegrations(), 'integrations')

function useLoader(fetcher) {
  const [state, setState] = useState({ data: [], loading: true, error: null })
  const load = useCallback(async () => {
    try {
      const data = await fetcher()
      setState({ data, loading: false, error: null })
    } catch (error) {
      setState((s) => ({ ...s, loading: false, error }))
    }
  }, [fetcher])
  useEffect(() => {
    load()
  }, [load])
  const reload = useCallback(() => {
    setState((s) => ({ ...s, loading: true, error: null }))
    load()
  }, [load])
  return [state, reload]
}

export default function DataPanel() {
  const documentsDemoOnly = isMocked('documents')
  const integrationsDemoOnly = isMocked('integrations')
  const [docs, reloadDocs] = useLoader(fetchDocuments)
  const [integrations, reloadIntegrations] = useLoader(fetchIntegrations)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef(null)
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState({ loading: false, error: null, results: null })

  async function handleFiles(fileList) {
    const files = Array.from(fileList ?? [])
    if (!files.length) return
    setUploading(true)
    setUploadError(null)
    try {
      for (const file of files) await api.uploadDocument(file)
    } catch (err) {
      setUploadError(err)
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
      reloadDocs()
    }
  }

  async function runSearch(event) {
    event.preventDefault()
    const q = query.trim()
    if (!q) return
    setSearch({ loading: true, error: null, results: null })
    try {
      const data = await api.searchDocuments(q)
      setSearch({ loading: false, error: null, results: asList(data, 'results') })
    } catch (error) {
      setSearch({ loading: false, error, results: null })
    }
  }

  return (
    <Card
      title="Connected data"
      subtitle={documentsDemoOnly || integrationsDemoOnly ? 'Some data below is simulated for demonstration.' : 'Documents and integrations the agent can draw on, with sources preserved.'}
    >
      <div className="space-y-5">
        {(documentsDemoOnly || integrationsDemoOnly) && (
          <p role="status" className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            {[
              documentsDemoOnly && 'Document upload, listing, and search are simulated; files are not stored or analyzed.',
              integrationsDemoOnly && 'Integration availability is demo data, not a live connection check.',
            ].filter(Boolean).join(' ')}
          </p>
        )}
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragging(false)
            handleFiles(e.dataTransfer.files)
          }}
          className={`rounded-lg border-2 border-dashed p-4 text-center transition ${dragging ? 'border-violet-500 bg-violet-500/10' : 'border-slate-700'}`}
        >
          <p className="text-sm text-slate-300">Drop PDF, DOCX or TXT files here</p>
          <input ref={fileInput} type="file" accept={ACCEPT} multiple className="hidden" onChange={(e) => handleFiles(e.target.files)} />
          <Button size="sm" variant="secondary" className="mt-2" loading={uploading} onClick={() => fileInput.current?.click()}>
            {uploading ? 'Uploading…' : 'Choose files'}
          </Button>
        </div>
        {uploadError && <ErrorState error={uploadError} compact title="Upload failed" />}

        <div>
          <SectionHeader title={`Documents (${docs.data.length})`} onRefresh={reloadDocs} />
          {docs.loading ? (
            <LoadingState label="Loading documents…" />
          ) : docs.error ? (
            <ErrorState error={docs.error} onRetry={reloadDocs} title="Could not load documents" />
          ) : docs.data.length === 0 ? (
            <EmptyState title="No documents yet" hint="Upload files so the agent can cite them." />
          ) : (
            <ul className="space-y-1.5">
              {docs.data.map((d, i) => {
                const name = d.filename ?? d.name ?? d.file_name ?? 'Untitled'
                return (
                  <li key={d.document_id ?? d.id ?? `${name}-${i}`} className="flex items-center justify-between gap-2 rounded-lg px-3 py-2 ring-1 ring-slate-800">
                    <div className="min-w-0">
                      <p className="truncate text-sm text-slate-100">📄 {name}</p>
                      <p className="text-xs text-slate-500">{[formatBytes(d.size_bytes), formatDateTime(d.uploaded_at)].filter(Boolean).join(' · ')}</p>
                    </div>
                    {d.status && <StatusBadge status={d.status} />}
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Search documents</h3>
          <form onSubmit={runSearch} className="flex gap-2">
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="e.g. Q3 revenue" />
            <Button type="submit" loading={search.loading} disabled={!query.trim()}>
              Search
            </Button>
          </form>
          <div className="mt-2">
            {search.error && <ErrorState error={search.error} compact title="Search failed" />}
            {search.results &&
              (search.results.length === 0 ? (
                <EmptyState title="No matches" hint="Try different keywords." />
              ) : (
                <ul className="space-y-2">
                  {search.results.map((r, i) => (
                    <li key={i} className="rounded-lg p-3 text-sm ring-1 ring-slate-800">
                      <p className="text-slate-200">“{r.excerpt ?? r.text ?? r.content ?? r.evidence?.excerpt}”</p>
                      <p className="mt-1 text-xs text-slate-500">
                        {[r.filename ?? r.source_name ?? r.evidence?.source_name, r.location ?? r.page, typeof r.score === 'number' ? `score ${r.score}` : null]
                          .filter(Boolean)
                          .join(' · ')}
                      </p>
                    </li>
                  ))}
                </ul>
              ))}
          </div>
        </div>

        <div>
          <SectionHeader title="Integrations" onRefresh={reloadIntegrations} />
          {integrations.loading ? (
            <LoadingState label="Loading integrations…" />
          ) : integrations.error ? (
            <ErrorState error={integrations.error} onRetry={reloadIntegrations} title="Could not load integrations" />
          ) : integrations.data.length === 0 ? (
            <EmptyState title="No integrations configured" />
          ) : (
            <ul className="space-y-1.5">
              {integrations.data.map((it, i) => (
                <li key={it.integration_id ?? it.id ?? i} className="flex items-start justify-between gap-2 rounded-lg px-3 py-2 ring-1 ring-slate-800">
                  <div className="min-w-0">
                    <p className="text-sm text-slate-100">{it.name ?? it.integration_id ?? it.id}</p>
                    {it.description && <p className="text-xs text-slate-500">{it.description}</p>}
                  </div>
                  <StatusBadge status={it.status ?? (it.available ? 'available' : 'unavailable')} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  )
}

function SectionHeader({ title, onRefresh }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      <Button size="sm" variant="ghost" onClick={onRefresh}>
        Refresh
      </Button>
    </div>
  )
}
