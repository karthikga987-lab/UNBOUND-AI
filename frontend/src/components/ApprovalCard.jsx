import { describeApproval } from '../lib/approval'
import { Button, JsonBlock } from './ui'

// Displays a backend-issued ApprovalRequest and forwards the user's decision to the backend.
// The frontend never approves anything itself and offers no way to edit the arguments:
// a changed action must come back from the backend as a new approval request.
export default function ApprovalCard({ approval, busy, onDecide }) {
  const approving = busy === `approve:${approval.approval_id}`
  const rejecting = busy === `reject:${approval.approval_id}`
  const details = describeApproval(approval)

  return (
    <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3" data-testid="approval-card">
      <p className="text-sm font-semibold text-amber-200">Approval required before this runs</p>
      <p className="mt-1 text-sm text-slate-200">{approval.description}</p>
      <p className="mt-1 text-xs text-slate-400">
        Tool <code className="text-violet-300">{approval.tool_name}</code> · step {approval.step_id}
      </p>

      <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-[max-content_1fr] sm:gap-x-3">
        {details.destination && (
          <>
            <dt className="text-slate-400">Website / destination</dt>
            <dd className="break-all text-slate-100">{details.destination}</dd>
          </>
        )}
        {details.amount && (
          <>
            <dt className="text-slate-400">Amount</dt>
            <dd className="text-base font-semibold text-amber-200" data-testid="approval-amount">{details.amount}</dd>
          </>
        )}
        <dt className="text-slate-400">What happens</dt>
        <dd className="text-slate-200">{details.consequence}</dd>
      </dl>

      {details.submittedData != null && (
        <>
          <p className="mt-3 text-xs text-slate-400">Data that will be submitted:</p>
          <JsonBlock value={details.submittedData} />
        </>
      )}
      <p className="mt-3 text-xs text-slate-400">It will run with exactly these parameters (secrets are hidden):</p>
      <JsonBlock value={details.parameters} />
      <p className="mt-2 text-[11px] text-slate-500">To change anything, reject it. A changed action needs a new approval from the backend.</p>

      <div className="mt-3 flex gap-2">
        <Button size="sm" variant="success" loading={approving} disabled={Boolean(busy)} onClick={() => onDecide(approval, 'approved')}>
          Approve
        </Button>
        <Button size="sm" variant="danger" loading={rejecting} disabled={Boolean(busy)} onClick={() => onDecide(approval, 'rejected')}>
          Reject
        </Button>
      </div>
    </div>
  )
}