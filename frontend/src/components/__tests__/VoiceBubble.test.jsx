import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import VoiceBubble from '../VoiceBubble'

const audioRef = { current: { kind: null, micLevel: 0, boundaryAt: 0 } }
const setup = (props) =>
  render(<VoiceBubble status="listening" transcript="" message="" audioRef={audioRef} onTap={vi.fn()} onToggleMute={vi.fn()} onEnd={vi.fn()} {...props} />)

afterEach(cleanup)

describe('VoiceBubble', () => {
  it.each([
    ['listening', 'Listening'],
    ['processing', 'Working on it…'],
    ['speaking', 'Speaking'],
    ['muted', 'Microphone muted'],
    ['unavailable', 'Voice unavailable'],
  ])('labels the %s state', (status, label) => {
    setup({ status })
    expect(screen.getByText(label)).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Voice mode' }).dataset.state).toBe(status)
  })

  it('shows the live transcript while listening', () => {
    setup({ transcript: 'compare the reports' })
    expect(screen.getByText('You: “compare the reports”')).toBeTruthy()
  })

  it('ends voice mode from the button and from Escape', () => {
    const onEnd = vi.fn()
    setup({ onEnd })
    fireEvent.click(screen.getByRole('button', { name: 'End voice' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onEnd).toHaveBeenCalledTimes(2)
  })

  it('hides mute when voice is unavailable and explains why', () => {
    setup({ status: 'unavailable', message: 'Microphone access is blocked.' })
    expect(screen.queryByRole('button', { name: 'Mute' })).toBeNull()
    expect(screen.getByText('Microphone access is blocked.')).toBeTruthy()
  })

  it('reports no audio when the hook provides none', () => {
    const { container } = setup({ status: 'speaking' })
    expect(container.querySelector('.voice-orb').dataset.audio).toBe('none')
  })
})
