// Merges browser live events and orchestrator task events into one chronological, de-duplicated feed.
export function buildActivity(liveEvents = [], taskEvents = []) {
  const items = []
  const seen = new Set()

  for (const ev of liveEvents) {
    const id = `browser:${ev.event_id}`
    if (seen.has(id)) continue
    seen.add(id)
    items.push({ id, time: ev.time, timestamp: ev.timestamp, type: ev.event_type, description: ev.description, source: 'browser', status: ev.verification?.status ?? ev.action_status ?? null })
  }

  for (const ev of Array.isArray(taskEvents) ? taskEvents : []) {
    const time = Date.parse(ev?.timestamp ?? '')
    if (!ev?.event_id || Number.isNaN(time)) continue
    const id = `task:${ev.event_id}`
    if (seen.has(id)) continue
    seen.add(id)
    items.push({ id, time, timestamp: ev.timestamp, type: ev.event_type, description: ev.message, source: 'task', status: ev.status ?? null })
  }

  // Array.prototype.sort is stable, so equal timestamps keep their source order.
  return items.sort((a, b) => a.time - b.time)
}