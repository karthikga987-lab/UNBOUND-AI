import { act, cleanup, render, screen } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'

// Fake Web Speech APIs: we drive recognition results and finish utterances by hand.
const recs = []
const spoken = []
beforeAll(() => {
  window.SpeechRecognition = class {
    constructor() {
      recs.push(this)
    }
    start() {}
    stop() {
      this.onend?.()
    }
    abort() {}
  }
  window.speechSynthesis = {
    speak: (u) => {
      spoken.push(u)
      setTimeout(() => u.onstart?.(), 0)
    },
    cancel: () => spoken.at(-1)?.onend?.(),
    getVoices: () => [],
  }
  window.SpeechSynthesisUtterance = class {
    constructor(text) {
      this.text = text
    }
  }
  window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
})
afterAll(cleanup)
afterEach(() => vi.restoreAllMocks())

const state = () => screen.queryByRole('region', { name: 'Voice mode' })?.dataset.state ?? 'closed'
const tick = (ms = 50) => act(() => new Promise((r) => setTimeout(r, ms)))
async function say(text) {
  const rec = recs.at(-1)
  await act(async () => {
    rec.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] })
    rec.onend()
  })
}
async function waitFor(check, ms = 4000) {
  const until = Date.now() + ms
  while (!check() && Date.now() < until) await tick()
  expect(check()).toBe(true)
}
const finishSpeaking = async () => {
  await waitFor(() => state() === 'speaking')
  await act(async () => spoken.at(-1).onend())
}

it('replies with the real plan, answers commands, and the orb interrupts speech', async () => {
  vi.resetModules()
  const { api } = await import('../../api')
  vi.spyOn(api, 'createConversation').mockResolvedValue({ session_id: 'voice-flow-session' })
  vi.spyOn(api, 'getConversation').mockResolvedValue({ session_id: 'voice-flow-session', messages: [] })
  vi.spyOn(api, 'classifyAssistantRequest').mockResolvedValue({
    category: 'action_execution',
    current_turn_authorizes_execution: true,
  })
  vi.spyOn(api, 'assistantRequest').mockImplementation(async (message) => ({
    intent: { category: 'action_execution', current_turn_authorizes_execution: true },
    reply: 'Task plan is ready. Nothing has been executed.',
    task: await api.createTask(message),
  }))
  const { default: App } = await import('../../App')
  render(<App />)
  await act(async () => screen.getByLabelText('Start voice mode').click())
  expect(state()).toBe('listening')

  await say('compare the website report with my documents')
  await waitFor(() => spoken.length >= 1)
  expect(spoken[0].text).toMatch(/5-step plan\. First, I'll open the reports website/)
  expect(spoken[0].text).toMatch(/Say "run it"/)
  await finishSpeaking()
  await waitFor(() => state() === 'listening')

  await say('status')
  await waitFor(() => spoken.length >= 2)
  expect(spoken[1].text).toBe('The plan is ready with 5 steps. Say "run it" to start.')

  // Tapping the orb while it talks stops it and goes back to listening.
  await waitFor(() => state() === 'speaking')
  await act(async () => screen.getByRole('button', { name: 'Stop talking and listen' }).click())
  await waitFor(() => state() === 'listening')

  await say('run it')
  await waitFor(() => spoken.some((u) => u.text === 'Running it now.'))
  await finishSpeaking()
  // Real progress from the (mock) backend is narrated as it happens.
  await waitFor(() => spoken.some((u) => /^Step \d of 5:|need your go-ahead/.test(u.text)), 8000)
}, 20000)
