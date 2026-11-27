import { useEffect, useMemo, useRef } from 'react'
import { buildActivity } from '../../live/activity'
import { readableEventType } from '../../live/contract'
import { formatTime } from '../../lib/format'
import { Card, EmptyState, StatusBadge } from '../ui'

export default function ActivityTimeline({ liveEvents, taskEvents }) {
  const items = useMemo(() => buildActivity(liveEvents, taskEvents), [liveEvents, taskEvents])
  const listRef = useRef(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [items.length])

  return (
    <Card title="Activity" subtitle={`${items.length} event${items.length === 1 ? '' : 's'}, oldest first`}>
      {items.length === 0 ? (
        <EmptyState title="No activity yet" hint="Browser and task events appear here as they happen." />
      ) : (
        <div
          ref={listRef}
          role="region"
          aria-label="Activity events"
          tabIndex={0}
          className="max-h-80 overflow-y-auto rounded-lg pr-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/60"
        >
          <ol className="space-y-1.5 text-xs" data-testid="activity-list">
            {items.map((item) => (
              <li key={item.id} className="rounded-md px-2 py-1.5 ring-1 ring-slate-800" data-testid="activity-item">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-slate-500">{formatTime(item.timestamp)}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${item.source === 'browser' ? 'bg-cyan-500/15 text-cyan-300' : 'bg-violet-500/15 text-violet-300'}`}>
                    {item.source}
                  </span>
                  <span className="text-slate-400">{readableEventType(item.type)}</span>
                  {item.status && <StatusBadge status={item.status} />}
                </div>
                <p className="mt-0.5 break-words text-slate-200">{item.description}</p>
              </li>
            ))}
          </ol>
        </div>
      )}
    </Card>
  )
}
