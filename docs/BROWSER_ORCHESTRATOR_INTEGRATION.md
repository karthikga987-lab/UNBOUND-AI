# Browser orchestrator integration

## Cancellation and browser replay dispatch

TaskManager cancellation is coordinated with replay action claim and browser
dispatch by the per-task `replay_dispatch_lock(task_id)`. A second cancellation
check without the shared synchronization cannot close the race.

The browser-aware executor holds this TaskManager interface around one complete
replay registry call:

```python
with task_manager.replay_dispatch_lock(task_id):
    if task_manager.is_cancelled(task_id):
        do_not_dispatch()
    replay_state_store.claim_action(...)
    dispatch_exactly_one_browser_action()
    persist_action_outcome_or_uncertainty()
```

`TaskManager.cancel(task_id)` acquires the same per-task replay dispatch lock
while changing the task to `CANCELLED`. If cancellation wins the lock, the
check prevents claim and dispatch. If dispatch wins, cancellation waits until
the single action has a persisted result or an `outcome_unknown` state, then
marks the task cancelled. The existing `execution_lock(task_id)` wraps a whole
executor run and is not a substitute if it would prevent cancellation from
interrupting unrelated work. Do not retry an action whose outcome is uncertain.

## Implemented integration seam

`backend.app.orchestrator.browser_integration.register_browser_integration()`
constructs the browser workflow store, task-scoped session manager, encrypted
replay store, replay bridge, replay engine, adapter, and TaskExecutor around the
existing TaskManager and ToolRegistry. It registers exactly the six bindings
returned by `browser_tool_bindings(adapter)`. The host application must call
this factory with its existing manager/registry and configured replay-state
encryption key; this repository currently has no application entrypoint that
invokes it automatically.

The TaskExecutor checks `ReplayExecutionResult.status`, preserves the parent
ToolCall ID, pauses while TaskManager has an action approval pending, and uses
an ephemeral TaskManager-issued registry approval capability for a trusted
resume. This capability is not a second user approval and is claimed once
before execution. Each resume still passes through ToolRegistry. TaskManager
public task/approval copies redact replay parameters, form values, continuation
internals, and filesystem paths while executor code uses private task reads.

`BrowserOrchestratorIntegration.shutdown()` closes sessions and drops active
recorder bookkeeping. TaskManager terminal callbacks perform corresponding
cleanup on completion, cancellation, failure, and needs-attention transitions.
Encrypted replay checkpoints survive process restart, but TaskManager and
bridge associations remain process-local. If the trusted parent call or bridge
continuation is unavailable, TaskExecutor marks replay as needing attention
and does not start it again.
