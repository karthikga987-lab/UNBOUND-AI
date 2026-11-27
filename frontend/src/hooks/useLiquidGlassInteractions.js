import { useEffect } from 'react'

/** Applies the shared glass shine and a position-aware water ripple to app buttons. */
export function useLiquidGlassInteractions(rootSelector = '.unbound-app') {
  useEffect(() => {
    const timers = new Map()
    const launchRipple = (button, x, y) => {
      if (!button || button.disabled || !button.isConnected) return
      const rect = button.getBoundingClientRect()
      const size = Math.max(36, Math.hypot(rect.width, rect.height) * 1.8)
      const ripple = document.createElement('span')
      ripple.className = 'liquid-water-ripple'
      ripple.setAttribute('aria-hidden', 'true')
      ripple.style.left = `${x - rect.left}px`
      ripple.style.top = `${y - rect.top}px`
      ripple.style.width = `${size}px`
      ripple.style.height = `${size}px`
      button.append(ripple)

      const remove = () => {
        const timer = timers.get(ripple)
        if (timer) window.clearTimeout(timer)
        timers.delete(ripple)
        ripple.remove()
      }
      ripple.addEventListener('animationend', remove, { once: true })
      timers.set(ripple, window.setTimeout(remove, 1000))
    }

    const onPointerDown = (event) => {
      if (event.button !== 0) return
      const root = event.target?.closest?.(rootSelector)
      const button = event.target?.closest?.('button')
      if (!root || !button) return
      launchRipple(button, event.clientX, event.clientY)
    }

    const onKeyDown = (event) => {
      if (event.repeat || (event.key !== 'Enter' && event.key !== ' ')) return
      const root = event.target?.closest?.(rootSelector)
      const button = event.target?.closest?.('button')
      if (!root || !button || button.disabled) return
      const rect = button.getBoundingClientRect()
      launchRipple(button, rect.left + rect.width / 2, rect.top + rect.height / 2)
    }

    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('keydown', onKeyDown, true)
      for (const [ripple, timer] of timers) {
        window.clearTimeout(timer)
        ripple.remove()
      }
      timers.clear()
    }
  }, [rootSelector])
}
