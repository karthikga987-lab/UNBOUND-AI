import { Button, JsonBlock } from './ui'

export default function ApprovalCard({ approval, busy, onDecide }) {
  const approving = busy === `approve:${approval.approval_id}`
  const rejecting = busy === `reject:${approval.approval_id}`
  return (
    <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3">
      <p className="text-sm font-semibold text-amber-200">Approval required before this runs</p>
      <p className="mt-1 text-sm text-slate-200">{approval.description}</p>
      <p className="mt-1 text-xs text-slate-400">
        Tool <code className="text-violet-300">{approval.tool_name}</code> · step {approval.step_id}
      </p>
      <p className="mt-2 text-xs text-slate-400">It will run with exactly these arguments:</p>
      <JsonBlock value={approval.arguments} />
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