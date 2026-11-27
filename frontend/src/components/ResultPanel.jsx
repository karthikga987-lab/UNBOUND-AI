import { useId, useState } from 'react'
import { API_BASE_URL } from '../api'
import { isTerminal, TaskStatus } from '../api/contracts'
import { formatDateTime } from '../lib/format'
import { Button, Card, EmptyState, JsonBlock, StatusBadge } from './ui'

const KNOWN_KEYS = ['summary', 'assistant_summary', 'assistant_summary_status', 'verified_facts', 'inferences', 'conflicts', 'missing_information', 'missing_info', 'assumptions', 'requested', 'actions_completed', 'verified', 'failed_or_incomplete', 'skipped_steps', 'assumptions_or_missing_information']
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
  const [expanded, setExpanded] = useState(false)
  const contentId = useId()
  const evidence = task.evidence ?? []
  const artifacts = task.artifacts ?? []
  const evidenceIndex = new Map(evidence.map((e, i) => [e.evidence_id, i + 1]))
  const result = task.result
  const documentAnalyses = getVerifiedDocumentAnalyses(result)
  const done = isTerminal(task.status)
  const summary = typeof result === 'string' ? result : result?.assistant_summary ?? result?.summary
  const browserObservations = getVerifiedBrowserObservations(result)
  const hasFullContent = result != null || artifacts.length > 0 || evidence.length > 0
  const extra =
    result && typeof result === 'object'
      ? Object.fromEntries(Object.entries(result).filter(([k]) => !KNOWN_KEYS.includes(k)))
      : {}

  return (
    <Card
      title="Results"
      subtitle={done ? 'Final outcome with evidence' : 'Updates live as evidence is gathered'}
      actions={<StatusBadge status={task.status} />}
    >
      <div className="space-y-3" data-testid="result-panel">
        {!hasFullContent ? (
          done ? (
            <EmptyState
              title={task.status === TaskStatus.COMPLETED ? 'No result returned' : `Task ${task.status.replaceAll('_', ' ')}`}
              hint="Any evidence gathered before it stopped is listed below."
            />
          ) : (
            <EmptyState title="Result not ready yet" hint="The summary and report appear here once execution finishes." />
          )
        ) : (
          <>
            <div className="rounded-xl border border-white/10 bg-white/[0.035] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]" data-testid="result-preview">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">Result preview</p>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                  {evidence.length > 0 && <span>{evidence.length} evidence item{evidence.length === 1 ? '' : 's'}</span>}
                  {artifacts.length > 0 && <span>{artifacts.length} artifact{artifacts.length === 1 ? '' : 's'}</span>}
                </div>
              </div>
              {summary ? (
                <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-slate-200">{summary}</p>
              ) : (
                <p className="mt-2 text-sm leading-relaxed text-slate-300">
                  {result != null ? 'Detailed result information is available.' : 'Saved evidence and artifacts are available to review.'}
                </p>
              )}
            </div>
            <Button
              variant="secondary"
              size="sm"
              className="w-full justify-between border border-white/10 bg-white/5 px-3 py-2 text-slate-200 hover:bg-white/10"
              aria-expanded={expanded}
              aria-controls={contentId}
              onClick={() => setExpanded((value) => !value)}
              data-testid="result-toggle"
            >
              <span>{expanded ? 'Hide full result' : 'View full result'}</span>
              <svg
                aria-hidden="true"
                className={`h-4 w-4 transition-transform duration-300 motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`}
                viewBox="0 0 20 20"
                fill="none"
              >
                <path d="m5 7.5 5 5 5-5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Button>
            <div
              id={contentId}
              className={`grid overflow-hidden transition-[grid-template-rows,opacity] duration-300 ease-in-out motion-reduce:transition-none ${expanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0'}`}
              aria-hidden={!expanded}
              inert={!expanded}
              data-testid="result-details"
            >
              <div className="min-h-0 overflow-hidden">
                <FullResult
                  task={task}
                  result={result}
                  summary={summary}
                  browserObservations={browserObservations}
                  documentAnalyses={documentAnalyses}
                  evidenceIndex={evidenceIndex}
                  extra={extra}
                  done={done}
                />
              </div>
            </div>
          </>
        )}
      </div>
    </Card>
  )
}

function FullResult({ task, result, summary, browserObservations, documentAnalyses, evidenceIndex, extra, done }) {
  return (
    <div className="space-y-5 border-t border-white/10 pt-3">
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
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-200">{summary}</p>
        </Section>
      )}
      {result?.assistant_summary_status?.startsWith('unavailable:') && (
        <p role="status" className="text-xs text-amber-300">A local AI summary was unavailable; the verified browser observation remains below.</p>
      )}
      {browserObservations.map((observation, index) => (
        <Section key={`${observation.url}-${index}`} title="Verified browser observation">
          <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-900/50 p-3">
            {isLocalDemoObservation(observation) && <p className="text-[10px] font-semibold uppercase tracking-wide text-cyan-300">LOCAL DEMO MODE · fixed synthetic fixture only · public browsing disabled</p>}
            <p className="text-sm font-medium text-slate-100">{observation.title || 'Page title unavailable'}</p>
            <a className="break-all text-xs text-sky-300 hover:underline" href={observation.url} target="_blank" rel="noreferrer">{observation.url}</a>
            <p className="whitespace-pre-wrap text-sm text-slate-300">{observation.visible_text}</p>
            {observation.screenshot_format === 'image/png' && typeof observation.screenshot_base64 === 'string' && (
              <figure className="space-y-1">
                <img className="max-h-96 max-w-full rounded border border-slate-700" src={`data:image/png;base64,${observation.screenshot_base64}`} alt={`Browser screenshot of ${observation.title || observation.url}`} loading="lazy" />
                {observation.screenshot_sha256 && <figcaption className="break-all font-mono text-[10px] text-slate-500">PNG SHA-256: {observation.screenshot_sha256}</figcaption>}
              </figure>
            )}
            {observation.truncated && <p className="text-xs text-amber-300">Visible page text was truncated to the configured bound.</p>}
            <p className="text-[10px] uppercase tracking-wide text-amber-300">Untrusted page content · data only</p>
          </div>
        </Section>
      ))}
      {documentAnalyses.map((analysis) => (
        <DocumentAnalysis key={analysis.documentId} analysis={analysis} evidenceIndex={evidenceIndex} />
      ))}
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
      <Artifacts artifacts={task.artifacts ?? []} />
      <Evidence evidence={task.evidence ?? []} />
    </div>
  )
}

function isLocalDemoObservation(observation) {
  return typeof observation?.url === 'string' && /^http:\/\/unbound-demo\.invalid\/(?:product|details)\/?$/.test(observation.url)
}

function getVerifiedBrowserObservations(result) {
  if (!Array.isArray(result?.verified)) return []
  return result.verified.flatMap((entry) => {
    const step = entry?.verification
    const data = step?.verified === true ? step?.tool_result : null
    const observation = data?.page_observation
    if (!observation || observation.content_trust !== 'untrusted_webpage_data') return []
    return [observation]
  })
}

function getVerifiedDocumentAnalyses(result) {
  if (!Array.isArray(result?.verified)) return []
  return result.verified.flatMap((entry) => {
    const output = entry?.verification?.tool_result
    const data = output?.data
    const analysis = data?.analysis
    if (data?.analysis_status !== 'verified_quotes' || !analysis || !Array.isArray(analysis.findings)) return []
    return [{
      documentId: data.document_id ?? entry.step_id,
      filename: data.original_filename ?? 'Local document',
      summary: analysis.summary,
      findings: analysis.findings,
      missing: Array.isArray(analysis.missing_or_unreadable) ? analysis.missing_or_unreadable : [],
      scope: data.analysis_scope,
    }]
  })
}

function DocumentAnalysis({ analysis, evidenceIndex }) {
  const scope = analysis.scope
  const hasPartialScope = scope && scope.complete_document_coverage === false
  return (
    <Section title={`Verified document analysis · ${analysis.filename}`}>
      <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-900/50 p-3">
        {analysis.summary && <p className="text-sm leading-relaxed text-slate-200">{analysis.summary}</p>}
        {scope && (
          <p className={`text-xs ${hasPartialScope ? 'text-amber-300' : 'text-slate-400'}`}>
            {hasPartialScope
              ? `Partial source scope: analyzed ${scope.selected_chunk_count} of ${scope.total_chunk_count} extracted chunks.`
              : `Source scope: all ${scope.total_chunk_count} extracted chunks were included.`}
          </p>
        )}
        <ul className="space-y-3">
          {analysis.findings.map((finding, index) => (
            <li key={`${finding.source_chunk_id}-${index}`} className="border-l-2 border-violet-500/60 pl-3 text-sm text-slate-200">
              <p><span className="mr-1.5 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] uppercase text-slate-400">{finding.kind}</span>{finding.statement}</p>
              {finding.quote && <blockquote className="mt-1 text-xs italic text-slate-400">“{finding.quote}”</blockquote>}
              {evidenceIndex.has(finding.evidence_id) && (
                <a href={`#evidence-${finding.evidence_id}`} className="mt-1 inline-block text-xs font-semibold text-violet-300 hover:underline">
                  Source [E{evidenceIndex.get(finding.evidence_id)}]{finding.section ? ` · ${finding.section}` : ''}
                </a>
              )}
            </li>
          ))}
        </ul>
        {analysis.missing.length > 0 && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2">
            <p className="text-xs font-semibold text-amber-200">Not found in the supplied excerpts</p>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-slate-300">
              {analysis.missing.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
            </ul>
          </div>
        )}
      </div>
    </Section>
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
