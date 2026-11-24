import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ResultPanel from '../ResultPanel'

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

    expect(screen.getByText('The page identifies Demo Product as version 2.1.')).toBeTruthy()
    expect(screen.getByText('Verified browser observation')).toBeTruthy()
    expect(screen.getByText(/LOCAL DEMO MODE · fixed synthetic fixture only · public browsing disabled/)).toBeTruthy()
    expect(screen.getByText(/Untrusted page content · data only/)).toBeTruthy()
  })
})
