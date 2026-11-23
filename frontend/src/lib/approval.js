// Presentation helpers for approval requests. This does NOT decide anything: approvals are created,
// validated and enforced by the backend orchestrator. The frontend only displays and forwards the decision.
import { maskSensitive } from '../live/contract'

const firstString = (...values) => values.find((v) => typeof v === 'string' && v.trim()) ?? null

const CONSEQUENCES = [
  [/email|message|send|notify/i, 'This sends a message on your behalf. Sent messages cannot be recalled.'],
  [/purchase|payment|pay|checkout|buy|order|book/i, 'This may charge money or create a binding booking or order.'],
  [/replay|workflow/i, 'The saved workflow will repeat its recorded actions on the live website, which may change data there.'],
  [/submit|fill|form/i, 'This submits information to an external website.'],
  [/click/i, 'This click may change data on an external website.'],
]

export function describeApproval(approval) {
  const args = approval?.arguments && typeof approval.arguments === 'object' ? approval.arguments : {}
  const hint = `${approval?.tool_name ?? ''} ${args.action ?? ''} ${approval?.description ?? ''}`

  const destination = firstString(args.url, args.destination, args.start_url, args.target?.url, args.recipient, args.to, args.email, args.site)
  const rawAmount = args.amount ?? args.price ?? args.total
  const amount = rawAmount != null && rawAmount !== '' ? `${rawAmount}${args.currency ? ` ${args.currency}` : ''}` : null
  const submitted = args.data ?? args.form_data ?? args.fields ?? args.body ?? args.payload ?? null

  return {
    destination,
    amount,
    submittedData: submitted == null ? null : maskSensitive(submitted),
    parameters: maskSensitive(args),
    consequence: CONSEQUENCES.find(([pattern]) => pattern.test(hint))?.[1] ?? 'This action can change things outside UNBOUND and may not be reversible.',
  }
}