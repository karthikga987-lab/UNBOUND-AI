import { useEffect, useState } from 'react'
import { formatTime } from '../../lib/format'
import { Button, Spinner } from '../ui'

const STALE_AFTER_MS = 15000

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

function Placeholder({ status, onRetry }) {
  const content = {
    unavailable: {
      title: 'Live view is not available yet',
      hint: 'The backend does not provide a browser session stream yet, so there is nothing real to show. No simulated activity is displayed.',
    },
    connecting: { title: 'Connecting to the browser session…', spinner: true },
    connected: { title: 'Waiting for the first screenshot…', spinner: true },
    reconnecting: { title: 'Connection lost. Reconnecting…', spinner: true },
    disconnected: { title: 'Disconnected from the browser session', hint: 'Automatic reconnection gave up.', retry: true },
    ended: { title: 'The browser session has ended.', hint: 'No screenshot was received for this session.' },
  }[status] ?? { title: 'No browser session' }

  return (
    <div className="absolute inset-0 grid place-items-center p-6 text-center" data-testid={`live-placeholder-${status}`}>
      <div className="max-w-sm space-y-2">
        {content.spinner && (
          <div className="flex justify-center text-violet-300">
            <Spinner />
          </div>
        )}
        <p className="text-sm font-medium text-slate-200">{content.title}</p>
        {content.hint && <p className="text-xs text-slate-400">{content.hint}</p>}
        {content.retry && (
          <Button size="sm" variant="secondary" onClick={onRetry}>
            Reconnect
          </Button>
        )}
      </div>
    </div>
  )
}

// Shows the latest real screenshot at the browser's own aspect ratio. Overlays are passed as children
// and are positioned in percentages of the screenshot, so they line up at any display size.
export default function ScreenshotViewer({ screenshot, status, onRetry, children }) {
  const now = useNow()
  const ratio = screenshot?.width && screenshot?.height ? `${screenshot.width} / ${screenshot.height}` : '16 / 10'
  const ageMs = screenshot ? now - screenshot.captured_at : 0
  const stale = Boolean(screenshot) && status === 'connected' && ageMs > STALE_AFTER_MS
  const offline = Boolean(screenshot) && status !== 'connected'

  return (
    <div className="relative w-full overflow-hidden rounded-lg bg-black ring-1 ring-slate-800" style={{ aspectRatio: ratio }}>
      {screenshot ? (
        <>
          <img src={screenshot.src} alt="Live browser screen" draggable={false} className="absolute inset-0 h-full w-full select-none object-contain" data-testid="live-screenshot" />
          {children}
        </>
      ) : (
        <Placeholder status={status} onRetry={onRetry} />
      )}

      {screenshot && (
        <div className="absolute right-2 top-2 flex flex-wrap justify-end gap-1 text-[11px]">
          {stale && (
            <span className="rounded bg-amber-500/90 px-1.5 py-0.5 font-medium text-slate-950" data-testid="stale-indicator">
              Last frame {Math.round(ageMs / 1000)}s old
            </span>
          )}
          <span className="rounded bg-slate-950/80 px-1.5 py-0.5 text-slate-300">Captured {formatTime(new Date(screenshot.captured_at).toISOString())}</span>
        </div>
      )}
      {offline && (
        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-slate-950/85 px-3 py-2 text-xs text-slate-200" data-testid="offline-banner">
          <span>
            {status === 'ended' ? 'Session ended, showing the last frame.' : status === 'reconnecting' ? 'Reconnecting… showing the last frame received.' : 'Not connected, showing the last frame received.'}
          </span>
          {status === 'disconnected' && (
            <Button size="sm" variant="secondary" onClick={onRetry}>
              Reconnect
            </Button>
          )}
        </div>
      )}
    </div>
  )
}