const TYPING_VISIBLE_MS = 4000

const pct = (value, total) => `${Math.min(100, Math.max(0, (value / total) * 100))}%`

// Draws only what the backend reported: cursor position, click location, target element and typing.
// Coordinates are in screenshot pixels; without the screenshot's width/height they cannot be placed,
// so nothing is drawn rather than guessing.
export default function ActionOverlay({ width, height, cursor, lastClick, target, typing, lastEventAt }) {
  if (!width || !height) return null
  const bbox = target?.bbox
  const showTyping = typing && lastEventAt != null && lastEventAt - typing.time <= TYPING_VISIBLE_MS

  return (
    <div className="pointer-events-none absolute inset-0" data-testid="action-overlay" aria-hidden="true">
      {bbox && (
        <div
          className="absolute rounded border-2 border-cyan-400/90 bg-cyan-400/10"
          style={{ left: pct(bbox.x, width), top: pct(bbox.y, height), width: pct(bbox.width, width), height: pct(bbox.height, height) }}
          data-testid="target-box"
        >
          {target.label && <span className="absolute -top-5 left-0 whitespace-nowrap rounded bg-cyan-400 px-1 text-[10px] font-medium text-slate-950">{target.label}</span>}
        </div>
      )}

      {lastClick && (
        <span
          key={lastClick.event_id}
          className="click-ripple absolute -ml-4 -mt-4 h-8 w-8 rounded-full border-2 border-violet-300 bg-violet-400/40"
          style={{ left: pct(lastClick.x, width), top: pct(lastClick.y, height) }}
          data-testid="click-indicator"
        />
      )}

      {cursor && (
        <svg className="absolute h-5 w-5 drop-shadow" style={{ left: pct(cursor.x, width), top: pct(cursor.y, height) }} viewBox="0 0 14 20" data-testid="cursor">
          <path d="M0 0 L0 16 L4.5 12 L7.5 19 L10 18 L7 11 L12.5 11 Z" fill="white" stroke="black" strokeWidth="1" />
        </svg>
      )}

      {showTyping && (
        <div className="absolute inset-x-3 bottom-3 flex justify-center" data-testid="typing-indicator">
          <span className="max-w-full truncate rounded-lg bg-slate-950/90 px-3 py-1.5 text-xs text-slate-100 ring-1 ring-violet-500/60">
            ⌨ Typing{typing.field_label ? ` in “${typing.field_label}”` : ''}
            {typing.text_preview ? `: ${typing.text_preview}` : ''}
          </span>
        </div>
      )}
    </div>
  )
}