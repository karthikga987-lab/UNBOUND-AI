export function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function formatDateTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

export function formatBytes(n) {
  if (typeof n !== 'number') return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}

// Endpoints without a shared contract may return an array or { items: [...] }.
export function asList(data, ...keys) {
  if (Array.isArray(data)) return data
  for (const key of [...keys, 'items', 'results', 'data']) {
    if (Array.isArray(data?.[key])) return data[key]
  }
  return []
}