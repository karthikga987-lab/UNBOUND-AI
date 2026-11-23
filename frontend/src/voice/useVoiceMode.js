import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Voice mode built on the browser's Web Speech APIs.
 *
 *   status: 'off' | 'listening' | 'processing' | 'speaking' | 'muted' | 'unavailable'
 *
 * Audio activity (`audioRef.current`) is only ever filled from real signals:
 *   - kind 'mic'    → live RMS level of the microphone while listening (Web Audio AnalyserNode)
 *   - kind 'speech' → timestamp of the last word boundary reported by speechSynthesis while speaking
 *   - kind null     → no audio data available; consumers must fall back to state-only animation
 * speechSynthesis exposes no output level, so word boundaries are the closest honest signal.
 *
 * Spoken output goes through a small queue so announcements never cut each other off.
 * Items marked `replaceable` (e.g. "Step 3 of 5…") are dropped when a newer one arrives.
 */

const SpeechRecognitionImpl = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : undefined
const synth = typeof window !== 'undefined' ? window.speechSynthesis : undefined

export const voiceSupport = {
  recognition: Boolean(SpeechRecognitionImpl),
  synthesis: Boolean(synth),
}

const BLOCKING_ERRORS = {
  'not-allowed': 'Microphone access is blocked. Allow it in your browser to use voice mode.',
  'service-not-allowed': 'Speech recognition is not allowed in this browser.',
  'audio-capture': 'No microphone was found.',
  network: 'The speech recognition service could not be reached. Voice input works in Chrome and Edge.',
}

const NO_AUDIO = { kind: null, micLevel: 0, boundaryAt: 0 }

// Prefer a natural-sounding English voice: neural/online voices first, British English next.
function pickVoice() {
  const voices = synth?.getVoices?.() ?? []
  const english = voices.filter((v) => /^en[-_]/i.test(v.lang))
  const score = (v) =>
    (/natural|neural|online/i.test(v.name) ? 4 : 0) +
    (/en[-_]gb/i.test(v.lang) ? 2 : 0) +
    (/google uk english male|ryan|george|daniel|thomas/i.test(v.name) ? 1 : 0)
  return english.sort((a, b) => score(b) - score(a))[0] ?? null
}

export function useVoiceMode({ onUtterance }) {
  const [status, setStatus] = useState('off')
  const [transcript, setTranscript] = useState('')
  const [message, setMessage] = useState('')

  const statusRef = useRef('off')
  const mutedRef = useRef(false)
  const transcriptRef = useRef('')
  const recognitionRef = useRef(null)
  const meterRef = useRef(null) // { stream, ctx, raf }
  const audioRef = useRef(NO_AUDIO)
  const queueRef = useRef([]) // [{ text, replaceable }]
  const drainingRef = useRef(false)
  const utteranceRef = useRef(null) // Chrome can garbage-collect utterances and never fire onend.
  const listenRef = useRef(() => {})
  const drainRef = useRef(() => {})
  const handleUtteranceRef = useRef(() => {})
  const onUtteranceRef = useRef(onUtterance)
  useEffect(() => {
    onUtteranceRef.current = onUtterance
  }, [onUtterance])

  const setPhase = useCallback((next) => {
    statusRef.current = next
    setStatus(next)
  }, [])
  const isOn = () => statusRef.current !== 'off' && statusRef.current !== 'unavailable'
  const updateTranscript = (t) => {
    transcriptRef.current = t
    setTranscript(t)
  }

  // ── Microphone level meter (real input level, listening only) ──────────────
  const stopMeter = useCallback(() => {
    const m = meterRef.current
    meterRef.current = null
    if (m) {
      cancelAnimationFrame(m.raf)
      m.stream.getTracks().forEach((t) => t.stop())
      m.ctx.close().catch(() => {})
    }
    if (audioRef.current.kind === 'mic') audioRef.current = NO_AUDIO
  }, [])

  const startMeter = useCallback(async () => {
    if (meterRef.current || !navigator.mediaDevices?.getUserMedia || !window.AudioContext) return
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
    } catch {
      return // Recognition reports permission problems itself; the bubble just stays state-driven.
    }
    if (statusRef.current !== 'listening' || meterRef.current) {
      stream.getTracks().forEach((t) => t.stop())
      return
    }
    const ctx = new AudioContext()
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    ctx.createMediaStreamSource(stream).connect(analyser)
    const buf = new Float32Array(analyser.fftSize)
    const m = { stream, ctx, raf: 0 }
    meterRef.current = m
    audioRef.current = { kind: 'mic', micLevel: 0, boundaryAt: 0 }
    const tick = () => {
      analyser.getFloatTimeDomainData(buf)
      let sum = 0
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
      audioRef.current.micLevel = Math.min(1, Math.sqrt(sum / buf.length) * 6)
      m.raf = requestAnimationFrame(tick)
    }
    tick()
  }, [])

  // ── Speech recognition ─────────────────────────────────────────────────────
  const stopRecognition = useCallback(() => {
    const rec = recognitionRef.current
    recognitionRef.current = null
    if (rec) {
      rec.onresult = rec.onend = rec.onerror = null
      try {
        rec.abort()
      } catch {
        /* already stopped */
      }
    }
    stopMeter()
  }, [stopMeter])

  const listen = useCallback(() => {
    if (!SpeechRecognitionImpl) {
      setMessage('Voice input is not supported in this browser. Try Chrome or Edge.')
      setPhase('unavailable')
      return
    }
    stopRecognition()
    updateTranscript('')
    setPhase('listening')

    const rec = new SpeechRecognitionImpl()
    rec.lang = navigator.language || 'en-US'
    rec.interimResults = true
    rec.continuous = false
    let finalText = ''

    rec.onresult = (event) => {
      let interim = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const r = event.results[i]
        if (r.isFinal) finalText += r[0].transcript
        else interim += r[0].transcript
      }
      updateTranscript((finalText + interim).trim())
    }
    rec.onerror = (event) => {
      const blocking = BLOCKING_ERRORS[event.error]
      if (blocking) {
        stopRecognition()
        setMessage(blocking)
        setPhase('unavailable')
      }
      // 'no-speech' and 'aborted' fall through to onend, which restarts listening.
    }
    rec.onend = () => {
      if (recognitionRef.current !== rec) return
      recognitionRef.current = null
      stopMeter()
      const text = (finalText || transcriptRef.current).trim()
      if (text) handleUtteranceRef.current(text)
      else if (queueRef.current.length) drainRef.current()
      else if (statusRef.current === 'listening') listenRef.current() // silence timeout: keep listening
    }

    recognitionRef.current = rec
    try {
      rec.start()
    } catch {
      setMessage('Could not start the microphone.')
      setPhase('unavailable')
      return
    }
    startMeter()
  }, [setPhase, startMeter, stopMeter, stopRecognition])
  useEffect(() => {
    listenRef.current = listen
  }, [listen])

  // After speaking or processing, go back to whatever the mic state should be.
  const resume = useCallback(() => {
    if (!isOn()) return
    if (mutedRef.current) setPhase('muted')
    else listen()
  }, [listen, setPhase])

  // ── Speech synthesis ───────────────────────────────────────────────────────
  const speak = useCallback(
    (text) =>
      new Promise((resolve) => {
        if (!text || !isOn()) return resolve()
        setMessage(text)
        if (!synth) return resolve() // Nothing can be said aloud; the text is shown instead.
        stopRecognition() // Don't let the assistant hear itself.
        const u = new SpeechSynthesisUtterance(text)
        const voice = pickVoice()
        if (voice) u.voice = voice
        u.lang = voice?.lang || navigator.language || 'en-US'
        u.rate = 1.03
        u.onstart = () => {
          if (!isOn()) return
          audioRef.current = NO_AUDIO
          setPhase('speaking')
        }
        u.onboundary = () => {
          audioRef.current = { kind: 'speech', micLevel: 0, boundaryAt: performance.now() }
        }
        let settled = false
        const done = () => {
          if (settled) return
          settled = true
          audioRef.current = NO_AUDIO
          resolve()
        }
        u.onend = done
        u.onerror = done
        utteranceRef.current = u
        synth.speak(u)
      }),
    [setPhase, stopRecognition],
  )

  // Speak everything queued, one after another, then return to listening.
  const drain = useCallback(async () => {
    if (drainingRef.current) return
    drainingRef.current = true
    while (queueRef.current.length && isOn() && statusRef.current !== 'processing') {
      await speak(queueRef.current.shift().text)
    }
    drainingRef.current = false
    if (isOn() && statusRef.current !== 'processing') resume()
  }, [resume, speak])
  useEffect(() => {
    drainRef.current = drain
  }, [drain])

  const handleUtterance = useCallback(
    async (text) => {
      setPhase('processing')
      let reply
      try {
        reply = await onUtteranceRef.current?.(text)
      } catch {
        reply = 'Sorry, I could not do that.'
      }
      if (!isOn()) return
      // The direct reply goes first; anything announced meanwhile follows it.
      if (reply) queueRef.current.unshift({ text: reply, replaceable: false })
      statusRef.current = 'idle' // leave 'processing' so the queue can drain
      drain()
    },
    [drain, setPhase],
  )
  useEffect(() => {
    handleUtteranceRef.current = handleUtterance
  }, [handleUtterance])

  // ── Public controls ────────────────────────────────────────────────────────
  const start = useCallback(() => {
    mutedRef.current = false
    queueRef.current = []
    setMessage('')
    listen()
  }, [listen])

  const stop = useCallback(() => {
    stopRecognition()
    queueRef.current = []
    synth?.cancel()
    mutedRef.current = false
    audioRef.current = NO_AUDIO
    updateTranscript('')
    setMessage('')
    setPhase('off')
  }, [setPhase, stopRecognition])

  const toggleMute = useCallback(() => {
    const s = statusRef.current
    if (!isOn()) return
    mutedRef.current = !mutedRef.current
    if (mutedRef.current) {
      stopRecognition()
      if (s === 'listening') setPhase('muted')
    } else if (s === 'muted') {
      listen()
    }
  }, [listen, setPhase, stopRecognition])

  /**
   * One tap on the orb does the obvious thing for the current state:
   * speaking → stop talking and listen; listening → I'm done, send it;
   * muted → unmute; unavailable → try again.
   */
  const tap = useCallback(() => {
    const s = statusRef.current
    if (s === 'speaking') {
      queueRef.current = []
      synth?.cancel() // the current utterance settles; drain then resumes listening
    } else if (s === 'listening') {
      if (transcriptRef.current) recognitionRef.current?.stop()
    } else if (s === 'muted') {
      toggleMute()
    } else if (s === 'unavailable') {
      start()
    }
  }, [start, toggleMute])

  /** Queue an announcement (e.g. a task update). Spoken when the user isn't mid-sentence. */
  const announce = useCallback(
    (text, { replaceable = false } = {}) => {
      if (!isOn() || !text) return
      if (replaceable) queueRef.current = queueRef.current.filter((item) => !item.replaceable)
      queueRef.current.push({ text, replaceable })
      const s = statusRef.current
      if (s === 'processing') return // handleUtterance drains afterwards
      if (s === 'listening' && transcriptRef.current) return // don't talk over the user; onend drains
      drain()
    },
    [drain],
  )

  useEffect(() => stop, [stop]) // Release mic + speech on unmount.

  return {
    active: status !== 'off',
    status,
    transcript,
    message,
    muted: status === 'muted',
    audioRef,
    start,
    stop,
    toggleMute,
    tap,
    announce,
  }
}
