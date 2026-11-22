import { API_BASE_URL } from '../api'
import { isTerminal, TaskStatus } from '../api/contracts'
import { formatDateTime } from '../lib/format'
import { Card, EmptyState, JsonBlock, StatusBadge } from './ui'

const KNOWN_KEYS = ['summary', 'verified_facts', 'inferences', 'conflicts', 'missing_information', 'missing_info', 'assumptions']
const TONES = { emerald: 'border-emerald-500/60', orange: 'border-orange-500/60', sky: 'border-sky-500/60', slate: 'border-slate-600' }
const BTN = 'rounded-lg bg-violet-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-violet-500'

const asArray = (v) => (Array.isArray(v) ? v : v ? [v] : [])
function toItem(entry) {
  if (typeof entry === 'string') return { statement: entry, evidence_ids: [] }
  return {
    statement: entry?.statement ?? entry?.text ?? entry?.description ?? JSON.stringify(entry),
    evidence_ids: asArray(entry?.evidence_ids),
  }
}

export default function ResultPanel({ task }) {
  const evidenceIndex = new Map(task.evidence.map((e, i) => [e.evidence_id, i + 1]))
  const result = task.result
  const done = isTerminal(task.status)
  const summary = typeof result === 'string' ? result : result?.summary
  const extra =
    result && typeof result === 'object'
      ? Object.fromEntries(Object.entries(result).filter(([k]) => !KNOWN_KEYS.includes(k)))
      : {}

  return (
    <Card
      title="Result"
      subtitle={done ? 'Final outcome with evidence' : 'Updates live as evidence is gathered'}
      actions={<StatusBadge status={task.status} />}
    >
      <div className="space-y-5">
        {!result &&
          (done ? (
            <EmptyState
              title={task.status === TaskStatus.COMPLETED ? 'No result returned' : `Task ${task.status.replaceAll('_', ' ')}`}
              hint="Any evidence gathered before it stopped is listed below."
            />
          ) : (
            <EmptyState title="Result not ready yet" hint="The summary and report appear here once execution finishes." />
          ))}

        {summary && (
          <Section title="Summary">
            <p className="text-sm leading-relaxed text-slate-200">{summary}</p>
          </Section>
        )}
        <ItemList title="Verified facts" tone="emerald" items={result?.verified_facts} evidenceIndex={evidenceIndex} />
        <ItemList title="Conflicts between sources" tone="orange" items={result?.conflicts} evidenceIndex={evidenceIndex} />
        <ItemList title="Inferences (not verified)" tone="sky" items={result?.inferences} evidenceIndex={evidenceIndex} />
        <ItemList
          title="Missing information & assumptions"
          tone="slate"
          items={[...asArray(result?.missing_information ?? result?.missing_info), ...asArray(result?.assumptions)]}
          evidenceIndex={evidenceIndex}
        />
        {Object.keys(extra).length > 0 && (
          <Section title="Other details">
            <JsonBlock value={extra} />
          </Section>
        )}
        <Artifacts artifacts={task.artifacts} />
        <Evidence evidence={task.evidence} />
      </div>
    </Card>
  )
}

function Section({ title, children }) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      {children}
    </div>
  )
}

function ItemList({ title, items, tone, evidenceIndex }) {
  const list = asArray(items).map(toItem)
  if (!list.length) return null
  return (
    <Section title={title}>
      <ul className="space-y-2">
        {list.map((item, i) => (
          <li key={i} className={`border-l-2 pl-3 text-sm text-slate-200 ${TONES[tone]}`}>
            {item.statement}
            {item.evidence_ids.map(
              (id) =>
                evidenceIndex.has(id) && (
                  <a key={id} href={`#evidence-${id}`} className="ml-1 align-super text-[10px] font-semibold text-violet-300 hover:underline">
                    [E{evidenceIndex.get(id)}]
                  </a>
                ),
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function resolveUrl(url) {
  return /^https?:\/\//i.test(url) ? url : `${API_BASE_URL}${url.startsWith('/') ? '' : '/'}${url}`
}

function downloadContent(artifact) {
  const body = typeof artifact.content === 'string' ? artifact.content : JSON.stringify(artifact.content, null, 2)
  const url = URL.createObjectURL(new Blob([body], { type: artifact.mime_type ?? 'text/plain' }))
  const link = document.createElement('a')
  link.href = url
  link.download = artifact.name ?? 'artifact.txt'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function Artifacts({ artifacts }) {
  if (!artifacts?.length) return null
  return (
    <Section title={`Generated artifacts (${artifacts.length})`}>
      <ul className="space-y-2">
        {artifacts.map((a, i) => {
          const href = a.url ?? a.download_url
          const name = a.name ?? a.filename ?? `Artifact ${i + 1}`
          return (
            <li key={a.artifact_id ?? i} className="rounded-lg p-3 ring-1 ring-slate-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-100">📄 {name}</p>
                  <p className="text-xs text-slate-500">{[a.type, a.mime_type].filter(Boolean).join(' · ')}</p>
                </div>
                {href ? (
                  <a href={resolveUrl(href)} target="_blank" rel="noreferrer" download className={BTN}>
                    Download
                  </a>
                ) : a.content != null ? (
                  <button type="button" onClick={() => downloadContent(a)} className={BTN}>
                    Download
                  </button>
                ) : (
                  <span className="text-xs text-slate-500">No file attached</span>
                )}
              </div>
              {typeof a.content === 'string' && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs text-slate-400">Preview</summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-xs text-slate-300">{a.content}</pre>
                </details>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function Evidence({ evidence }) {
  return (
    <Section title={`Evidence & sources (${evidence.length})`}>
      {evidence.length === 0 ? (
        <p className="text-sm text-slate-500">No evidence collected yet.</p>
      ) : (
        <ol className="space-y-2">
          {evidence.map((e, i) => (
            <li key={e.evidence_id} id={`evidence-${e.evidence_id}`} className="rounded-lg p-3 text-sm ring-1 ring-slate-800 target:ring-violet-500">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-violet-300">E{i + 1}</span>
                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-xs uppercase text-slate-300">{e.source_type}</span>
                <span className="font-medium text-slate-100">{e.source_name}</span>
              </div>
              <p className="mt-1 text-slate-300">Supports: {e.supports_claim}</p>
              {e.excerpt && <blockquote className="mt-1 border-l-2 border-slate-700 pl-2 text-xs italic text-slate-400">“{e.excerpt}”</blockquote>}
              <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500">
                {e.source_url && (
                  <a href={e.source_url} target="_blank" rel="noreferrer" className="break-all text-sky-400 hover:underline">
                    {e.source_url}
                  </a>
                )}
                {e.file_path && <span className="break-all font-mono">{e.file_path}</span>}
                <span>Retrieved {formatDateTime(e.retrieved_at)}</span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Section>
  )
}