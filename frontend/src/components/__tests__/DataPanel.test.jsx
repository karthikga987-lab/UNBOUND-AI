import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api', () => ({
  isMocked: vi.fn((area) => area === 'documents'),
  api: {
    listDocuments: vi.fn().mockResolvedValue([]),
    listIntegrations: vi.fn().mockResolvedValue([]),
  },
}))

import DataPanel from '../DataPanel'

afterEach(cleanup)

describe('document upload formats', () => {
  it('advertises only formats supported by the real backend extractor', () => {
    render(<DataPanel />)

    expect(screen.getByText('Drop PDF, DOCX or TXT files here')).toBeTruthy()
    expect(document.querySelector('input[type="file"]').accept).toBe('.pdf,.docx,.txt')
    expect(screen.getByRole('status').textContent).toMatch(/simulated; files are not stored or analyzed/i)
  })
})
