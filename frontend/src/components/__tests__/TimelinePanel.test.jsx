import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import TimelinePanel from '../TimelinePanel'

const task = {
  task_id: '4b243750-7d8c-47c3-ac9b-05651d82bf89',
  user_request: 'Search the uploaded material',
  status: 'needs_attention',
  plan: [],
  pending_approvals: [],
  completed_steps: [],
  evidence: [],
  artifacts: [],
}

describe('TimelinePanel execution diagnostics', () => {
  it('shows durable event metadata and sanitized diagnostic details', () => {
    render(<TimelinePanel
      task={task}
      events={[{
        event_id: 'event-1',
        event_type: 'search.extraction_failed',
        timestamp: '2026-11-27T12:00:00Z',
        status: 'failed',
        message: 'Search returned an invalid result shape',
        step_id: 's1',
        tool_name: 'document.search',
        duration_ms: 31,
        result_count: 2,
        error_category: 'search_extraction_failure',
        screenshot_reference: 'sha256:' + 'a'.repeat(64),
        browser_session_id: 'session-1',
        provider_session_id: 'provider-1',
        diagnostic: { document_ids: ['doc-1'], stage: 'normalized_response' },
      }]}
      error={null}
      busy={null}
      actionError={null}
    />)

    fireEvent.click(screen.getByText('Execution log (1 events)'))
    expect(screen.getByText('document.search')).toBeTruthy()
    expect(screen.getByText('31 ms')).toBeTruthy()
    expect(screen.getByText('2 results')).toBeTruthy()
    expect(screen.getByText('search_extraction_failure')).toBeTruthy()
    expect(screen.getByText(/session session-1/)).toBeTruthy()
    expect(screen.getByText(/provider provider-1/)).toBeTruthy()
    expect(screen.getByText(/screenshot sha256:/)).toBeTruthy()

    fireEvent.click(screen.getByText('Diagnostic details'))
    expect(screen.getByText(/normalized_response/)).toBeTruthy()
    expect(screen.getByText(/doc-1/)).toBeTruthy()
  })

  it('distinguishes baseline hosts from new exact hosts requiring Run plan', () => {
    render(<TimelinePanel
      task={{
        ...task,
        status: 'created',
        baseline_browser_domains: ['example.com', 'docs.python.org'],
        requested_browser_domains: ['example.com', 'github.com'],
        browser_domain_authorization_status: 'awaiting_run_plan',
      }}
      events={[]}
      busy={null}
      onExecute={() => {}}
    />)

    expect(screen.getByRole('region', { name: 'Website access for this plan' })).toBeTruthy()
    expect(screen.getByText('example.com')).toBeTruthy()
    expect(screen.getByText('Already allowed')).toBeTruthy()
    expect(screen.getByText('github.com')).toBeTruthy()
    expect(screen.getByText('Requires Run plan')).toBeTruthy()
    expect(screen.getByText('Review these destinations above before selecting Run plan.')).toBeTruthy()
  })
})
