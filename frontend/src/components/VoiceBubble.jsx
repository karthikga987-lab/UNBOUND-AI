import { useEffect, useRef } from 'react'

const LABELS = {
  listening: 'Listening',
  processing: 'Working on it…',
  speaking: 'Speaking',
  muted: 'Microphone muted',
  unavailable: 'Voice unavailable',
}

const HINTS = {
  listening: 'Ask for anything, or say “status”, “run it”, “read the results”.',
  processing: 'Planning your request.',
  muted: 'Tap the orb to unmute.',
}

// What tapping the orb does in each state (also its accessible name).
const TAP_ACTION = {
  listening: 'Send what I said',
  speaking: 'Stop talking and listen',
  muted: 'Unmute microphone',
  unavailable: 'Try voice again',
}

// How strongly real audio drives the orb in each state. Listening stays calm.
const GAIN = { listening: 0.45, speaking: 1 }

function usePrefersReducedMotion() {
  const ref = useRef(typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!mq) return
    const update = () => (ref.current = mq.matches)
    mq.addEventListener('change', update)
    return () => mq.removeEventListener('change', update)
  }, [])
  return ref
}

/**
 * Floating voice-mode bubble. Purely presentational: all state comes from useVoiceMode.
 * `audioRef` is read every frame; when it carries no real audio (kind === null), the
 * orb's level stays at 0 and only the CSS state animation runs.
 */
export default function VoiceBubble({ status, transcript, message, audioRef, onTap, onToggleMute, onEnd }) {
  const orbRef = useRef(null)
  const reducedMotion = usePrefersReducedMotion()

  // Drive --voice-level from real audio data without re-rendering React.
  useEffect(() => {
    const el = orbRef.current
    if (!el) return
    let raf = 0
    let shown = 0
    const tick = () => {
      const audio = audioRef.current
      const gain = GAIN[status] ?? 0
      let target = 0
      if (!reducedMotion.current && gain) {
        if (audio.kind === 'mic') target = audio.micLevel
        else if (audio.kind === 'speech') target = Math.exp(-(performance.now() - audio.boundaryAt) / 220)
      }
      target *= gain
      // Fast attack, slow release keeps it smooth instead of jittery.
      shown += (target - shown) * (target > shown ? 0.35 : 0.08)
      if (shown < 0.002) shown = 0
      el.style.setProperty('--voice-level', shown.toFixed(3))
      el.dataset.audio = audio.kind && gain ? 'live' : 'none'
      raf = requestAnimationFrame(tick)
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [status, audioRef, reducedMotion])

  // Esc ends voice mode, mirroring the End button.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onEnd()
    }
    window.addEventListener('keydown', onKey)
    document.documentElement.classList.add('voice-mode-active')
    return () => {
      window.removeEventListener('keydown', onKey)
      document.documentElement.classList.remove('voice-mode-active')
    }
  }, [onEnd])

  const muted = status === 'muted'
  const canMute = status !== 'unavailable'
  const tapAction = status === 'listening' && !transcript ? null : TAP_ACTION[status]
  const detail =
    (status === 'listening' || status === 'processing') && transcript
      ? `You: “${transcript}”`
      : status === 'speaking' || status === 'unavailable'
        ? message
        : HINTS[status]

  return (
    <div
      role="region"
      aria-label="Voice mode"
      className="voice-bubble fixed inset-x-3 bottom-3 z-30 sm:inset-x-auto sm:right-6 sm:bottom-6 sm:w-[380px]"
      data-state={status}
    >
      <div className="flex items-center gap-4 rounded-3xl border border-cyan-300/15 bg-slate-950/85 p-3 pr-4 shadow-2xl shadow-violet-950/40 backdrop-blur-md">
        <div className="voice-float shrink-0 p-3">
          <button
            type="button"
            onClick={onTap}
            disabled={!tapAction}
            aria-label={tapAction ?? LABELS[status]}
            title={tapAction ?? undefined}
            className="voice-orb-button rounded-full focus-visible:outline-2 focus-visible:outline-offset-8 focus-visible:outline-cyan-300"
          >
            <span className="voice-hud" aria-hidden="true">
              <span className="voice-hud-ring voice-hud-outer" />
              <span className="voice-hud-ring voice-hud-inner" />
            </span>
            <span ref={orbRef} className="voice-orb" aria-hidden="true">
              <span className="voice-orb-ring" />
              <span className="voice-orb-core" />
            </span>
          </button>
        </div>

        <div className="min-w-0 flex-1">
          <p aria-live="polite" className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <span className="voice-dot h-2 w-2 shrink-0 rounded-full" />
            {LABELS[status]}
          </p>
          {detail && <p className="mt-0.5 line-clamp-3 text-xs text-slate-400">{detail}</p>}
          <div className="mt-2 flex gap-2">
            {canMute && (
              <button
                type="button"
                onClick={onToggleMute}
                aria-pressed={muted}
                className="rounded-full border border-slate-700 px-3 py-1 text-xs font-medium text-slate-300 transition hover:border-violet-400 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-400"
              >
                {muted ? 'Unmute' : 'Mute'}
              </button>
            )}
            <button
              type="button"
              onClick={onEnd}
              className="rounded-full bg-violet-600 px-3 py-1 text-xs font-semibold text-white transition hover:bg-violet-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-400"
            >
              End voice
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
