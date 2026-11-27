import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import ResultPanel from '../ResultPanel'

afterEach(cleanup)

describe('verified local document results', () => {
  it('shows findings, exact source citations, and the analyzed source scope', () => {
    const task = {
      status: 'completed',
      result: {
        summary: 'The report recommends a phased upgrade.',
        verified: [{
          step_id: 'read_file',
          verification: {
            verified: true,
            tool_result: {
              data: {
                document_id: 'doc-1',
                original_filename: 'synthetic-report.docx',
                analysis_status: 'verified_quotes',
                analysis_scope: {
                  selected_chunk_count: 6,
                  total_chunk_count: 6,
                  omitted_chunk_count: 0,
                  complete_document_coverage: true,
                },
                analysis: {
                  summary: 'The document states a synthetic budget.',
                  findings: [{
                    kind: 'fact',
                    statement: 'The budget is INR 42 lakh.',
                    quote: 'Budget: INR 42 lakh',
                    evidence_id: 'evidence-1',
                    source_chunk_id: 'chunk-1',
                    section: 'Budget',
                  }],
                  missing_or_unreadable: ['Limitations were not found in the supplied excerpts.'],
                },
              },
            },
          },
        }],
      },
      evidence: [{
        evidence_id: 'evidence-1',
        source_type: 'document',
        source_name: 'synthetic-report.docx',
        supports_claim: 'Synthetic source excerpt',
        excerpt: 'Budget: INR 42 lakh',
        retrieved_at: '2026-11-24T10:00:00Z',
      }],
      artifacts: [],
    }

    render(<ResultPanel task={task} />)

    const toggle = screen.getByRole('button', { name: 'View full result' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByTestId('result-details').getAttribute('aria-hidden')).toBe('true')
    fireEvent.click(toggle)

    expect(screen.getByText('Verified document analysis · synthetic-report.docx')).toBeTruthy()
    expect(screen.getByText('The budget is INR 42 lakh.')).toBeTruthy()
    expect(screen.getAllByText(/Budget: INR 42 lakh/).some((element) => element.tagName === 'BLOCKQUOTE')).toBe(true)
    expect(screen.getByRole('link', { name: 'Source [E1] · Budget' }).getAttribute('href')).toBe('#evidence-evidence-1')
    expect(screen.getByText('Source scope: all 6 extracted chunks were included.')).toBeTruthy()
    expect(screen.getByText('Not found in the supplied excerpts')).toBeTruthy()
  })

  it('shows the generated summary and verified browser observation as untrusted page data', () => {
    const observation = {
      url: 'http://unbound-demo.invalid/details',
      title: 'Demo Product details',
      visible_text: 'Version: 2.1\nFeatures: document search, task planning, evidence summaries',
      truncated: false,
      content_trust: 'untrusted_webpage_data',
      screenshot_format: 'image/png',
      screenshot_base64: 'iVBORw0KGgo=',
      screenshot_sha256: 'a'.repeat(64),
    }
    render(<ResultPanel task={{
      status: 'completed',
      result: {
        assistant_summary: 'The page identifies Demo Product as version 2.1.',
        verified: [{ verification: { verified: true, tool_result: { task_id: 'task-1', page_observation: observation } } }],
      },
      evidence: [],
      artifacts: [],
    }} />)

    fireEvent.click(screen.getByRole('button', { name: 'View full result' }))

    const fullResult = within(screen.getByTestId('result-details'))
    expect(fullResult.getByText('The page identifies Demo Product as version 2.1.')).toBeTruthy()
    expect(fullResult.getByText('Verified browser observation')).toBeTruthy()
    expect(fullResult.getByText(/LOCAL DEMO MODE · fixed synthetic fixture only · public browsing disabled/)).toBeTruthy()
    expect(fullResult.getByText(/Untrusted page content · data only/)).toBeTruthy()
    expect(fullResult.getByRole('img', { name: 'Browser screenshot of Demo Product details' }).getAttribute('src')).toBe('data:image/png;base64,iVBORw0KGgo=')
    expect(fullResult.getByText(`PNG SHA-256: ${'a'.repeat(64)}`)).toBeTruthy()
  })

  it('keeps long result data intact behind an accessible compact preview and toggles the full result', () => {
    const longSummary = Array.from({ length: 80 }, (_, index) => `Verified finding ${index + 1} with source details.`).join('\n')
    render(<ResultPanel task={{
      status: 'completed',
      result: { summary: longSummary, verified_facts: ['A preserved fact.'] },
      evidence: [{
        evidence_id: 'e-long',
        source_type: 'webpage',
        source_name: 'Long report source',
        supports_claim: 'Supports the preserved result',
        excerpt: 'The full supporting excerpt remains available.',
        retrieved_at: '2026-11-27T10:00:00Z',
      }],
      artifacts: [],
    }} />)

    const toggle = screen.getByRole('button', { name: 'View full result' })
    const details = screen.getByTestId('result-details')
    expect(screen.getByTestId('result-preview').querySelector('p.line-clamp-3')).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(details.getAttribute('aria-hidden')).toBe('true')
    expect(details.hasAttribute('inert')).toBe(true)

    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Hide full result' }).getAttribute('aria-expanded')).toBe('true')
    expect(details.getAttribute('aria-hidden')).toBe('false')
    expect(details.hasAttribute('inert')).toBe(false)
    const fullResult = within(details)
    expect(details.querySelector('p.whitespace-pre-wrap')?.textContent).toBe(longSummary)
    expect(fullResult.getByText('A preserved fact.')).toBeTruthy()
    expect(fullResult.getByText(/The full supporting excerpt remains available\./)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Hide full result' }))
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(details.getAttribute('aria-hidden')).toBe('true')
    expect(details.hasAttribute('inert')).toBe(true)
  })
})
