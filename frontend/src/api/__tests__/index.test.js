import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('API implementation selection', () => {
  it('uses real backend endpoints by default when mocks are not configured', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_MOCK_AREAS', '')
    vi.resetModules()
    const { USE_MOCK, isMocked } = await import('../index')

    expect(USE_MOCK).toBe(false)
    expect(isMocked('tasks')).toBe(false)
    expect(isMocked('workflows')).toBe(false)
  })

  it('keeps selectively mocked demo areas explicit and leaves task APIs real', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_MOCK_AREAS', 'workflows,documents')
    vi.resetModules()
    const { isMocked, MOCK_AREAS } = await import('../index')

    expect(isMocked('tasks')).toBe(false)
    expect(isMocked('workflows')).toBe(true)
    expect(isMocked('documents')).toBe(true)
    expect([...MOCK_AREAS]).toEqual(['workflows', 'documents'])
  })
})
