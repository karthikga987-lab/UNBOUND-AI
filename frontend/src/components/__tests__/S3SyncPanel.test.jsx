import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const state = vi.hoisted(() => ({
  api: { s3SyncStatus: vi.fn(), listS3Backups: vi.fn(), syncToS3: vi.fn(), restoreTaskBackup: vi.fn() },
}))
vi.mock('../../api', () => ({ api: state.api }))

import S3SyncPanel from '../S3SyncPanel'

beforeEach(() => {
  vi.clearAllMocks()
  state.api.s3SyncStatus.mockResolvedValue({ enabled: false, status: 'disabled', pending_count: 0, retry_count: 0 })
  state.api.listS3Backups.mockResolvedValue([])
})
afterEach(cleanup)

describe('S3SyncPanel', () => {
  it('labels disabled sync and keeps upload unavailable', async () => {
    render(<S3SyncPanel />)
    expect(await screen.findByText(/Automatic sync disabled/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sync now' }).disabled).toBe(true)
    expect(screen.getByText(/Enable with UNBOUND_S3_SYNC_ENABLED=1/i)).toBeTruthy()
  })

  it('requires an explicit confirmation before a real sync request', async () => {
    state.api.s3SyncStatus.mockResolvedValue({ enabled: true, status: 'idle', pending_count: 0, retry_count: 0 })
    state.api.syncToS3.mockResolvedValue({ enabled: true, status: 'synchronized', pending_count: 0, retry_count: 0 })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<S3SyncPanel />)
    const button = await screen.findByRole('button', { name: 'Sync now' })
    await waitFor(() => expect(button.disabled).toBe(false))
    fireEvent.click(button)
    await waitFor(() => expect(state.api.syncToS3).toHaveBeenCalledTimes(1))
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('Upload a consistent snapshot'))
  })
})
