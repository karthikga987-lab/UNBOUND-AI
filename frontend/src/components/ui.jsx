const BADGE = {
  created: 'bg-slate-500/15 text-slate-300 ring-slate-500/30',
  pending: 'bg-slate-500/15 text-slate-300 ring-slate-500/30',
  planning: 'bg-sky-500/15 text-sky-300 ring-sky-500/30',
  running: 'bg-blue-500/15 text-blue-300 ring-blue-500/30',
  executing: 'bg-blue-500/15 text-blue-300 ring-blue-500/30',
  verifying: 'bg-indigo-500/15 text-indigo-300 ring-indigo-500/30',
  awaiting_approval: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
  recording: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  completed: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  approved: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  available: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  connected: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  ingested: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  online: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
  failed: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  rejected: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  error: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  offline: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  needs_attention: 'bg-orange-500/15 text-orange-300 ring-orange-500/30',
  cancelled: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
  skipped: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
  not_configured: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
  unavailable: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
  initializing: 'bg-sky-500/15 text-sky-300 ring-sky-500/30',
  waiting_for_approval: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
  paused: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
  cancelling: 'bg-orange-500/15 text-orange-300 ring-orange-500/30',
  connecting: 'bg-sky-500/15 text-sky-300 ring-sky-500/30',
  reconnecting: 'bg-orange-500/15 text-orange-300 ring-orange-500/30',
  disconnected: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
  ended: 'bg-zinc-500/15 text-zinc-300 ring-zinc-500/30',
  passed: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
}
const LIVE = new Set(['running', 'executing', 'verifying', 'planning', 'recording', 'checking', 'initializing', 'connecting', 'reconnecting', 'cancelling'])

export function StatusBadge({ status, label }) {
  const key = String(status ?? 'unknown')
  const cls = BADGE[key] ?? 'bg-slate-500/15 text-slate-300 ring-slate-500/30'
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${cls}`}>
      {LIVE.has(key) && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {label ?? key.replaceAll('_', ' ')}
    </span>
  )
}

export function Card({ title, subtitle, actions, children, className = '' }) {
  return (
    <section className={`rounded-xl border border-slate-800 bg-slate-900/60 shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-2 border-b border-slate-800 px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
            {subtitle && <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

const VARIANTS = {
  primary: 'bg-violet-600 text-white hover:bg-violet-500',
  secondary: 'bg-slate-800 text-slate-100 ring-1 ring-inset ring-slate-700 hover:bg-slate-700',
  danger: 'bg-rose-600 text-white hover:bg-rose-500',
  success: 'bg-emerald-600 text-white hover:bg-emerald-500',
  ghost: 'text-slate-300 hover:bg-slate-800',
}

export function Spinner() {
  return <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
}

export function Button({ variant = 'primary', size = 'md', loading = false, disabled, className = '', children, ...props }) {
  const sizes = size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-2 text-sm'
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${sizes} ${VARIANTS[variant]} ${className}`}
      {...props}
    >
      {loading && <Spinner />}
      {children}
    </button>
  )
}

export function Input({ className = '', ...props }) {
  return (
    <input
      {...props}
      className={`w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-violet-500 focus:outline-none ${className}`}
    />
  )
}

export function LoadingState({ label = 'Loading…' }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-slate-400">
      <Spinner /> {label}
    </div>
  )
}

export function EmptyState({ title, hint }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700 px-4 py-6 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  )
}

export function ErrorState({ error, onRetry, compact = false, title = 'Something went wrong' }) {
  const message = typeof error === 'string' ? error : error?.message ?? 'Unknown error'
  return (
    <div role="alert" className={`rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-200 ${compact ? 'px-3 py-2 text-xs' : 'p-4 text-sm'}`}>
      <p>
        <span className="font-semibold">{title}:</span> {message}
      </p>
      {onRetry && (
        <Button size="sm" variant="secondary" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function JsonBlock({ value }) {
  return (
    <pre className="mt-1 max-h-60 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-300 ring-1 ring-slate-800">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}