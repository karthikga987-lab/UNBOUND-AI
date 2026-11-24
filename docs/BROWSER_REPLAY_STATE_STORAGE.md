# Encrypted browser replay-state storage

`ReplayStateStore` keeps replay checkpoints in one SQLite file per UUID replay run under `backend/generated_data/replay_state/`. The full state record—including resolved parameters, exact prepared action arguments, action results, authorization-consumption records, workflow digest, and journal—is serialized in memory and encrypted with `BrowserReplayStateCipher` before SQLite receives it. The SQLite file and journal therefore contain ciphertext, not replay payload fields. Missing or invalid `BROWSER_REPLAY_STATE_KEY` prevents store construction; there is no plaintext fallback.

The store derives the task ID from the shared `ToolCall`, validates UUIDs before constructing filenames, and rejects symlinked storage paths. On POSIX it requests mode `0700` for the storage directory and `0600` for database and lock files. Windows permission behavior follows the directory's inherited ACL; Python's `chmod` does not provide POSIX-equivalent ACL control there.

## Checkpoint and authorization behavior

Each replay includes the workflow start navigation followed by the prepared workflow steps, in order. The first call only persists state and returns a safe pending-action descriptor. Exact arguments are available only through `ReplayPolicyContext`, whose sensitive field is omitted from normal representation and serialization; callers must keep this handoff inside trusted backend orchestration and must not log it or return it to a client.

`claim_action` requires an injected trusted authorization validator and a permit bound to the current task, tool call, replay, continuation, step, action digest, and expiry. It consumes the authorization ID and token hash and moves exactly one journal entry from `not_started` to `in_progress` in the same SQLite transaction that advances the checkpoint version. A per-run OS lock is held through dispatch; callers must complete the lease after a known success or mark it uncertain after an ambiguous result. A process crash releases the OS lock but leaves the durable `in_progress` state. `recover_after_restart` can then change it to `outcome_unknown` and `needs_attention`; it never retries it.

Completed steps advance the ordered cursor and are not claimable again. Workflow digest changes, session loss, stale continuation versions, expired/mismatched authorization, or uncertain outcomes prevent further claims. The browser store validates and consumes authorization; it does not create, approve, resolve, or manage TaskManager approvals.

## Concurrency and deployment limits

SQLite `BEGIN IMMEDIATE` transactions plus the per-run OS file lock coordinate concurrent processes on one local filesystem/host. This is not a distributed lock and does not guarantee correct coordination across multiple hosts or filesystems that do not implement reliable SQLite/advisory locking. Deploy one storage owner per local replay-state directory; multi-host deployments need a shared transactional store/locking service agreed with orchestration before use. The store does not resume Playwright sessions itself: a missing session is conservatively marked `needs_attention`, and continuation requires a separately verified session-continuity handoff.

The encryption key must be configured and rotated as described in [Browser replay state encryption](BROWSER_REPLAY_STATE_ENCRYPTION.md). The replay-state store does not generate keys or enforce the documented per-key encryption usage limit.
