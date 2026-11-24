"""Encrypted persistent checkpoints and one-use replay authorization claims.

Replay payloads are encrypted in full before they reach SQLite. SQLite uses
transactions for cross-process compare-and-set operations on one local database
file per UUID run. An OS file lock spans a claimed browser action so recovery can
distinguish a crashed action from one still executing on the same host.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, UUID4, ValidationError

from backend.app.browser.replay_state import (
    PreparedReplayAction,
    ReplayActionJournalState,
    ReplayActionOutcome,
    ReplayContinuation,
    ReplayExecutionResult,
    ReplayPendingAction,
    ReplayPolicyContext,
    ReplayStepAuthorization,
    ReplayStepCheckpoint,
)
from backend.app.browser.state_encryption import (
    BrowserReplayStateCipher,
    ReplayStateEncryptionError,
)
from backend.app.schemas.contracts import ToolCall, WorkflowDefinition


class ReplayStateStoreError(RuntimeError):
    """Base class for safe replay-state storage failures."""


class ReplayStateNotFoundError(ReplayStateStoreError):
    pass


class ReplayStateConflictError(ReplayStateStoreError):
    pass


class ReplayStateIntegrityError(ReplayStateStoreError):
    pass


class ReplayAuthorizationError(ReplayStateStoreError):
    pass


class ReplayStateBusyError(ReplayStateStoreError):
    pass


class ReplayWorkflowMismatchError(ReplayStateStoreError):
    pass


AuthorizationValidator = Callable[[ReplayStepAuthorization, ToolCall], bool]
SessionValidator = Callable[[UUID, UUID], bool]


class _StoredAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    action: str
    arguments: dict[str, Any] = Field(repr=False)
    digest_salt: str
    action_digest: str
    state: ReplayActionJournalState = ReplayActionJournalState.NOT_STARTED
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    result: dict[str, Any] | None = Field(default=None, repr=False)

    def public_checkpoint(self) -> ReplayStepCheckpoint:
        return ReplayStepCheckpoint(
            step_id=self.step_id,
            action=self.action,
            action_digest=self.action_digest,
            state=self.state,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


class _ReplayStatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: StrictInt = 1
    replay_run_id: UUID4
    continuation_id: UUID4
    task_id: UUID4
    session_id: UUID4
    workflow_id: UUID4
    workflow_digest: str
    workflow_digest_salt: str = Field(repr=False)
    tool_call_id: UUID4
    checkpoint_version: int = 0
    next_action_index: int = 0
    completed_step_ids: list[str] = Field(default_factory=list)
    uncertain_step_id: str | None = None
    needs_attention: bool = False
    safe_error: str | None = None
    resolved_parameters: dict[str, Any] = Field(repr=False)
    actions: list[_StoredAction] = Field(repr=False)
    consumed_authorization_hashes: list[str] = Field(default_factory=list, repr=False)
    consumed_authorization_ids: list[UUID4] = Field(default_factory=list, repr=False)


@dataclass
class ReplayActionLease:
    """Holds the per-run OS lock across dispatch until completion or uncertainty."""

    continuation: ReplayContinuation
    policy_context: ReplayPolicyContext = field(repr=False)
    _lock: "_InterProcessLock" = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if not self._released:
            self._lock.release()
            self._released = True

    def __enter__(self) -> "ReplayActionLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        # Leaving a claimed action incomplete keeps its durable in_progress state;
        # after lock release, recovery conservatively records outcome_unknown.
        self.release()


class _InterProcessLock:
    """Nonblocking advisory lock, released automatically by the OS on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
        except (OSError, ImportError):
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            raise ReplayStateBusyError("Replay action is already claimed") from None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            os.close(fd)


class ReplayStateStore:
    """Persist encrypted replay state under a controlled generated-data folder."""

    _TABLE = "replay_state"

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        cipher: BrowserReplayStateCipher | None = None,
        authorization_validator: AuthorizationValidator | None = None,
        session_validator: SessionValidator | None = None,
    ) -> None:
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(__file__).resolve().parents[2] / "generated_data" / "replay_state"
        )
        self.cipher = cipher or BrowserReplayStateCipher()
        # A missing validator fails closed at claim time. It is intentionally
        # injected by trusted orchestration code; this store creates no approvals.
        self.authorization_validator = authorization_validator
        self.session_validator = session_validator
        self._prepare_directory()

    def create(
        self,
        workflow: WorkflowDefinition,
        *,
        tool_call: ToolCall,
        session_id: UUID,
        resolved_parameters: Mapping[str, Any],
        prepared_actions: Sequence[PreparedReplayAction],
    ) -> ReplayExecutionResult:
        """Create encrypted state and publish only a safe descriptor for action 0."""
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError("workflow must be a WorkflowDefinition")
        if tool_call.tool_name != "browser.replay":
            raise ReplayStateStoreError("Replay state requires a browser.replay ToolCall")
        if not isinstance(resolved_parameters, Mapping) or not all(
            isinstance(name, str) for name in resolved_parameters
        ):
            raise ReplayStateStoreError("Replay parameters are invalid")
        raw_workflow_id = tool_call.arguments.get("workflow_id")
        try:
            call_workflow_id = (
                workflow.workflow_id
                if raw_workflow_id is None
                else UUID(str(raw_workflow_id))
            )
        except (TypeError, ValueError, AttributeError):
            raise ReplayStateStoreError("Replay call does not contain a valid workflow ID") from None
        if call_workflow_id != workflow.workflow_id:
            raise ReplayStateStoreError("Replay call does not match the selected workflow")
        call_parameters = tool_call.arguments.get("parameters")
        if call_parameters is not None:
            try:
                if self._canonical_json(dict(call_parameters)) != self._canonical_json(dict(resolved_parameters)):
                    raise ReplayStateStoreError("Replay call parameters do not match prepared state")
            except (TypeError, ValueError):
                raise ReplayStateStoreError("Replay call parameters are invalid") from None

        try:
            prepared: list[PreparedReplayAction] = [
                PreparedReplayAction(
                    step_id="workflow.start",
                    action="navigate",
                    arguments={"url": str(workflow.start_url)},
                ),
                *[
                    item if isinstance(item, PreparedReplayAction)
                    else PreparedReplayAction.model_validate(item)
                    for item in prepared_actions
                ],
            ]
        except (ValidationError, TypeError, ValueError):
            raise ReplayStateStoreError("Replay actions could not be safely prepared") from None
        ids = [item.step_id for item in prepared]
        if len(ids) != len(set(ids)):
            raise ReplayStateStoreError("Replay action step IDs must be unique")
        if any(not item.step_id.strip() or not item.action.strip() for item in prepared):
            raise ReplayStateStoreError("Replay actions require a step ID and action")

        try:
            resolved = json.loads(self._canonical_json(dict(resolved_parameters)))
            workflow_digest_salt = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
            workflow_digest = self._workflow_digest(workflow, workflow_digest_salt)
            actions = [self._stored_action(item) for item in prepared]
        except (TypeError, ValueError, ValidationError):
            raise ReplayStateStoreError("Replay state could not be safely prepared") from None

        replay_run_id = uuid4()
        payload = _ReplayStatePayload(
            replay_run_id=replay_run_id,
            continuation_id=uuid4(),
            task_id=tool_call.task_id,
            session_id=session_id,
            workflow_id=workflow.workflow_id,
            workflow_digest=workflow_digest,
            workflow_digest_salt=workflow_digest_salt,
            tool_call_id=tool_call.tool_call_id,
            resolved_parameters=resolved,
            actions=actions,
        )
        self._verify_action_digests(payload)
        path = self._database_path(replay_run_id)
        created = False
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
            created = True
            self._set_private_file_mode(path)
            connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            try:
                self._configure_connection(connection)
                connection.execute(
                    f"CREATE TABLE {self._TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), payload BLOB NOT NULL)"
                )
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"INSERT INTO {self._TABLE}(singleton, payload) VALUES(1, ?)",
                    (self.cipher.encrypt(payload.model_dump_json().encode("utf-8")),),
                )
                connection.commit()
            finally:
                connection.close()
            self._set_private_file_mode(path)
        except FileExistsError:
            raise ReplayStateConflictError("Replay run ID already exists") from None
        except Exception:
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ReplayStateStoreError("Replay state could not be created safely") from None
        return self._result(payload)

    def load_result(
        self,
        replay_run_id: UUID | str,
        *,
        current_workflow: WorkflowDefinition,
    ) -> ReplayExecutionResult:
        """Load a safe status snapshot and persist attention on workflow mismatch."""
        payload = self._load(replay_run_id)
        mismatch = self._workflow_mismatch(payload, current_workflow)
        if mismatch:
            payload = self._mark_attention(payload.replay_run_id, mismatch)
        return self._result(payload)

    def get_pending_policy_context(
        self,
        continuation: ReplayContinuation,
        *,
        tool_call: ToolCall,
        current_workflow: WorkflowDefinition,
    ) -> ReplayPolicyContext:
        """Return exact arguments only for the trusted internal policy handoff."""
        payload = self._load(continuation.replay_run_id)
        self._assert_current(payload, continuation, tool_call)
        mismatch = self._workflow_mismatch(payload, current_workflow)
        if mismatch:
            self._mark_attention(payload.replay_run_id, mismatch)
            raise ReplayWorkflowMismatchError(mismatch)
        if payload.needs_attention or payload.uncertain_step_id:
            raise ReplayStateConflictError("Replay requires attention before it can continue")
        action = self._current_action(payload)
        if action.state != ReplayActionJournalState.NOT_STARTED:
            raise ReplayStateConflictError("Replay action is not pending")
        return self._policy_context(payload, action)

    def claim_action(
        self,
        continuation: ReplayContinuation,
        authorization: ReplayStepAuthorization,
        *,
        tool_call: ToolCall,
        current_workflow: WorkflowDefinition,
        active_session_id: UUID | None,
        now: datetime | None = None,
    ) -> ReplayActionLease:
        """Atomically consume a trusted permit and claim one action for dispatch.

        The returned lease must remain held during browser dispatch and be passed
        to ``complete_action`` or ``mark_action_outcome_unknown``. A process crash
        releases the OS lock while leaving the durable journal in_progress.
        """
        if self.authorization_validator is None:
            raise ReplayAuthorizationError("Trusted replay authorization is unavailable")
        try:
            trusted = self.authorization_validator(authorization, tool_call)
        except Exception:
            raise ReplayAuthorizationError("Trusted replay authorization is invalid") from None
        if trusted is not True:
            raise ReplayAuthorizationError("Trusted replay authorization is invalid")

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ReplayAuthorizationError("Replay authorization time is invalid")
        session_valid = False
        if self.session_validator is not None and active_session_id is not None:
            try:
                session_valid = self.session_validator(continuation.task_id, active_session_id) is True
            except Exception:
                session_valid = False
        token = authorization.one_time_token.get_secret_value()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        lock = self._acquire_run_lock(continuation.replay_run_id)
        try:
            workflow_mismatch = False
            session_mismatch = False
            with self._write_state(continuation.replay_run_id) as payload:
                self._assert_current(payload, continuation, tool_call)
                mismatch = self._workflow_mismatch(payload, current_workflow)
                if mismatch:
                    payload.needs_attention = True
                    payload.safe_error = mismatch
                    payload.checkpoint_version += 1
                    workflow_mismatch = True
                elif (
                    not session_valid
                    or active_session_id != payload.session_id
                ):
                    payload.needs_attention = True
                    payload.safe_error = "Browser session continuity could not be verified"
                    payload.checkpoint_version += 1
                    session_mismatch = True
                elif payload.needs_attention or payload.uncertain_step_id:
                    raise ReplayStateConflictError("Replay requires attention before it can continue")
                else:
                    action = self._current_action(payload)
                    self._validate_authorization(
                        authorization,
                        payload,
                        action,
                        tool_call,
                        instant,
                    )
                    if (
                        token_hash in payload.consumed_authorization_hashes
                        or authorization.authorization_id in payload.consumed_authorization_ids
                    ):
                        raise ReplayAuthorizationError("Replay authorization has already been consumed")
                    if action.state != ReplayActionJournalState.NOT_STARTED:
                        raise ReplayStateConflictError("Replay action cannot be claimed again")

                    action.state = ReplayActionJournalState.IN_PROGRESS
                    action.started_at = instant
                    payload.consumed_authorization_hashes.append(token_hash)
                    payload.consumed_authorization_ids.append(authorization.authorization_id)
                    payload.checkpoint_version += 1
                    new_continuation = self._continuation(payload)
                    context = self._policy_context(payload, action)
            if workflow_mismatch:
                raise ReplayWorkflowMismatchError("Stored workflow version changed; replay needs attention")
            if session_mismatch:
                raise ReplayStateConflictError("Browser session continuity could not be verified")
            return ReplayActionLease(new_continuation, context, lock)
        except Exception:
            lock.release()
            raise

    def complete_action(
        self,
        lease: ReplayActionLease,
        *,
        result: Mapping[str, Any],
        completed_at: datetime | None = None,
    ) -> ReplayExecutionResult:
        """Persist a successful result and advance progress before returning next."""
        if lease._released:
            raise ReplayStateConflictError("Replay action lease is no longer active")
        try:
            canonical_result = json.loads(self._canonical_json(dict(result)))
            instant = completed_at or datetime.now(timezone.utc)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("completion timestamp must be timezone-aware")
            with self._write_state(lease.continuation.replay_run_id) as payload:
                self._assert_continuation(payload, lease.continuation)
                action = self._current_action(payload)
                if action.step_id != lease.policy_context.step_id:
                    raise ReplayStateConflictError("Replay action lease does not match the current step")
                if action.state != ReplayActionJournalState.IN_PROGRESS:
                    raise ReplayStateConflictError("Replay action is not in progress")
                action.state = ReplayActionJournalState.COMPLETED
                action.completed_at = instant
                action.result = canonical_result
                payload.completed_step_ids.append(action.step_id)
                payload.next_action_index += 1
                payload.checkpoint_version += 1
                payload.uncertain_step_id = None
            return self._result(payload)
        except (TypeError, ValueError):
            raise ReplayStateStoreError("Replay action result could not be persisted safely") from None
        finally:
            lease.release()

    def mark_action_outcome_unknown(
        self,
        lease: ReplayActionLease,
    ) -> ReplayExecutionResult:
        """Record uncertainty after dispatch; the action is never auto-retried."""
        if lease._released:
            raise ReplayStateConflictError("Replay action lease is no longer active")
        try:
            with self._write_state(lease.continuation.replay_run_id) as payload:
                self._assert_continuation(payload, lease.continuation)
                action = self._current_action(payload)
                if action.step_id != lease.policy_context.step_id:
                    raise ReplayStateConflictError("Replay action lease does not match the current step")
                if action.state != ReplayActionJournalState.IN_PROGRESS:
                    raise ReplayStateConflictError("Replay action is not in progress")
                action.state = ReplayActionJournalState.OUTCOME_UNKNOWN
                payload.uncertain_step_id = action.step_id
                payload.needs_attention = True
                payload.safe_error = "The browser action outcome is uncertain; operator review is required"
                payload.checkpoint_version += 1
            return self._result(payload)
        finally:
            lease.release()

    def get_action_outcome(
        self,
        replay_run_id: UUID | str,
        step_id: str,
    ) -> ReplayActionOutcome:
        """Read a completed action result through an internal, non-serializing model."""
        payload = self._load(replay_run_id)
        action = next((item for item in payload.actions if item.step_id == step_id), None)
        if action is None or action.state != ReplayActionJournalState.COMPLETED or action.result is None:
            raise ReplayStateNotFoundError("Completed replay action result was not found")
        return ReplayActionOutcome(
            replay_run_id=payload.replay_run_id,
            step_id=action.step_id,
            action_digest=action.action_digest,
            result=action.result,
        )

    def recover_after_restart(self, replay_run_id: UUID | str) -> ReplayExecutionResult:
        """Mark abandoned in-progress action unknown after obtaining the OS lock.

        If another process still holds the action lease, recovery fails safely.
        This lock coordinates processes on one local filesystem/host, not hosts.
        """
        run_id = self._normalize_run_id(replay_run_id)
        lock = self._acquire_run_lock(run_id)
        try:
            with self._write_state(run_id) as payload:
                action = next(
                    (item for item in payload.actions if item.state == ReplayActionJournalState.IN_PROGRESS),
                    None,
                )
                if action is not None:
                    action.state = ReplayActionJournalState.OUTCOME_UNKNOWN
                    payload.uncertain_step_id = action.step_id
                    payload.needs_attention = True
                    payload.safe_error = "The browser action outcome is uncertain after recovery"
                    payload.checkpoint_version += 1
                return self._result(payload)
        finally:
            lock.release()

    def mark_session_lost(self, continuation: ReplayContinuation) -> ReplayExecutionResult:
        """Fail closed when session continuity is unavailable or cannot be verified."""
        lock = self._acquire_run_lock(continuation.replay_run_id)
        try:
            with self._write_state(continuation.replay_run_id) as payload:
                self._assert_continuation(payload, continuation)
                current = self._current_action(payload) if payload.next_action_index < len(payload.actions) else None
                if current and current.state == ReplayActionJournalState.IN_PROGRESS:
                    current.state = ReplayActionJournalState.OUTCOME_UNKNOWN
                    payload.uncertain_step_id = current.step_id
                payload.needs_attention = True
                payload.safe_error = "Browser session continuity could not be verified"
                payload.checkpoint_version += 1
                return self._result(payload)
        finally:
            lock.release()

    def mark_cancelled(self, continuation: ReplayContinuation) -> ReplayExecutionResult:
        """Durably stop a replay after cancellation without implying rollback.

        If an action is already in progress, its outcome is unknown and must be
        reviewed. Otherwise completed checkpoints remain completed and no later
        action can be claimed through this continuation.
        """
        lock = self._acquire_run_lock(continuation.replay_run_id)
        try:
            with self._write_state(continuation.replay_run_id) as payload:
                self._assert_continuation(payload, continuation)
                current = self._current_action(payload) if payload.next_action_index < len(payload.actions) else None
                if current and current.state == ReplayActionJournalState.IN_PROGRESS:
                    current.state = ReplayActionJournalState.OUTCOME_UNKNOWN
                    payload.uncertain_step_id = current.step_id
                    payload.safe_error = "Replay was cancelled during an action; operator review is required"
                else:
                    payload.safe_error = "Replay was cancelled; no later action was started"
                payload.needs_attention = True
                payload.checkpoint_version += 1
                return self._result(payload)
        finally:
            lock.release()

    def cancel_claimed_before_dispatch(self, lease: ReplayActionLease) -> ReplayExecutionResult:
        """Close a claimed action when cancellation is observed before dispatch."""
        if lease._released:
            raise ReplayStateConflictError("Replay action lease is no longer active")
        try:
            with self._write_state(lease.continuation.replay_run_id) as payload:
                self._assert_continuation(payload, lease.continuation)
                action = self._current_action(payload)
                if action.step_id != lease.policy_context.step_id or action.state != ReplayActionJournalState.IN_PROGRESS:
                    raise ReplayStateConflictError("Replay action lease does not match the current step")
                action.state = ReplayActionJournalState.NOT_STARTED
                action.started_at = None
                payload.needs_attention = True
                payload.safe_error = "Replay was cancelled before the browser action started"
                payload.checkpoint_version += 1
            return self._result(payload)
        finally:
            lease.release()

    @staticmethod
    def _workflow_digest(workflow: WorkflowDefinition, encoded_salt: str) -> str:
        """Hash canonical workflow JSON with a private per-run salt."""
        try:
            salt = base64.urlsafe_b64decode(encoded_salt + "=" * (-len(encoded_salt) % 4))
        except (ValueError, TypeError):
            raise ReplayStateIntegrityError("Stored workflow digest is invalid") from None
        if len(salt) != 32:
            raise ReplayStateIntegrityError("Stored workflow digest is invalid")
        encoded = ReplayStateStore._canonical_json(workflow.model_dump(mode="json"))
        return hashlib.sha256(salt + encoded.encode("utf-8")).hexdigest()

    def _validate_authorization(
        self,
        authorization: ReplayStepAuthorization,
        payload: _ReplayStatePayload,
        action: _StoredAction,
        tool_call: ToolCall,
        now: datetime,
    ) -> None:
        if (
            authorization.task_id != payload.task_id
            or authorization.tool_call_id != tool_call.tool_call_id
            or authorization.workflow_id != payload.workflow_id
            or authorization.replay_run_id != payload.replay_run_id
            or authorization.continuation_id != payload.continuation_id
            or authorization.step_id != action.step_id
            or authorization.action_digest != action.action_digest
            or tool_call.task_id != payload.task_id
            or tool_call.tool_call_id != payload.tool_call_id
            or tool_call.tool_name != "browser.replay"
        ):
            raise ReplayAuthorizationError("Replay authorization does not match the pending action")
        if authorization.issued_at > now or authorization.expires_at <= now:
            raise ReplayAuthorizationError("Replay authorization is expired or not yet valid")
        # Recompute the digest from the encrypted exact arguments before trusting it.
        if not self._action_digest_matches(action):
            raise ReplayStateIntegrityError("Stored replay action failed integrity validation")

    def _workflow_mismatch(
        self,
        payload: _ReplayStatePayload,
        workflow: WorkflowDefinition,
    ) -> str | None:
        try:
            actual_digest = self._workflow_digest(workflow, payload.workflow_digest_salt)
        except (TypeError, ValueError):
            return "Current workflow could not be validated"
        if workflow.workflow_id != payload.workflow_id or actual_digest != payload.workflow_digest:
            return "Stored workflow version changed; replay needs attention"
        return None

    def _mark_attention(self, replay_run_id: UUID, message: str) -> _ReplayStatePayload:
        lock = self._acquire_run_lock(replay_run_id)
        try:
            with self._write_state(replay_run_id) as payload:
                current = self._current_action(payload) if payload.next_action_index < len(payload.actions) else None
                if current and current.state == ReplayActionJournalState.IN_PROGRESS:
                    current.state = ReplayActionJournalState.OUTCOME_UNKNOWN
                    payload.uncertain_step_id = current.step_id
                    message = "The browser action outcome is uncertain; operator review is required"
                payload.needs_attention = True
                payload.safe_error = message
                payload.checkpoint_version += 1
            return payload
        finally:
            lock.release()

    def _policy_context(
        self,
        payload: _ReplayStatePayload,
        action: _StoredAction,
    ) -> ReplayPolicyContext:
        detached_arguments = json.loads(ReplayStateStore._canonical_json(action.arguments))
        return ReplayPolicyContext(
            task_id=payload.task_id,
            tool_call_id=payload.tool_call_id,
            replay_run_id=payload.replay_run_id,
            continuation_id=payload.continuation_id,
            workflow_id=payload.workflow_id,
            step_id=action.step_id,
            action=action.action,
            action_digest=action.action_digest,
            sequence_index=payload.next_action_index,
            exact_arguments=detached_arguments,
        )

    def _result(self, payload: _ReplayStatePayload) -> ReplayExecutionResult:
        continuation = self._continuation(payload)
        checkpoints = tuple(action.public_checkpoint() for action in payload.actions)
        action_in_progress = any(
            action.state == ReplayActionJournalState.IN_PROGRESS for action in payload.actions
        )
        if payload.needs_attention or payload.uncertain_step_id or action_in_progress:
            status = "needs_attention"
            pending = None
        elif payload.next_action_index >= len(payload.actions):
            status = "completed"
            pending = None
        else:
            status = "pending_action"
            pending = self._pending_action(payload)
        return ReplayExecutionResult(
            task_id=payload.task_id,
            replay_run_id=payload.replay_run_id,
            workflow_id=payload.workflow_id,
            session_id=payload.session_id,
            status=status,
            continuation=continuation,
            pending_action=pending,
            checkpoints=checkpoints,
            error=(
                payload.safe_error
                or ("Replay action is already claimed and cannot be repeated" if action_in_progress else None)
            ),
        )

    @staticmethod
    def _continuation(payload: _ReplayStatePayload) -> ReplayContinuation:
        return ReplayContinuation(
            replay_run_id=payload.replay_run_id,
            continuation_id=payload.continuation_id,
            task_id=payload.task_id,
            session_id=payload.session_id,
            workflow_id=payload.workflow_id,
            workflow_digest=payload.workflow_digest,
            checkpoint_version=payload.checkpoint_version,
            next_action_index=payload.next_action_index,
            completed_step_ids=tuple(payload.completed_step_ids),
            uncertain_step_id=payload.uncertain_step_id,
        )

    @staticmethod
    def _pending_action(payload: _ReplayStatePayload) -> ReplayPendingAction:
        item = payload.actions[payload.next_action_index]
        return ReplayPendingAction(
            replay_run_id=payload.replay_run_id,
            continuation_id=payload.continuation_id,
            workflow_id=payload.workflow_id,
            step_id=item.step_id,
            action=item.action,
            action_digest=item.action_digest,
            sequence_index=payload.next_action_index,
        )

    @staticmethod
    def _stored_action(action: PreparedReplayAction) -> _StoredAction:
        encoded = ReplayStateStore._canonical_json({
            "step_id": action.step_id,
            "action": action.action,
            "arguments": action.arguments,
        })
        salt = os.urandom(32)
        salt_encoded = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
        digest = hashlib.sha256(salt + encoded.encode("utf-8")).hexdigest()
        return _StoredAction(
            step_id=action.step_id,
            action=action.action,
            arguments=json.loads(ReplayStateStore._canonical_json(action.arguments)),
            digest_salt=salt_encoded,
            action_digest=digest,
        )

    @staticmethod
    def _action_digest_matches(action: _StoredAction) -> bool:
        try:
            salt = base64.urlsafe_b64decode(
                action.digest_salt + "=" * (-len(action.digest_salt) % 4)
            )
            encoded = ReplayStateStore._canonical_json({
                "step_id": action.step_id,
                "action": action.action,
                "arguments": action.arguments,
            })
        except (TypeError, ValueError):
            return False
        expected = hashlib.sha256(salt + encoded.encode("utf-8")).hexdigest()
        return len(salt) == 32 and secrets_compare(expected, action.action_digest)

    @staticmethod
    def _verify_action_digests(payload: _ReplayStatePayload) -> None:
        if any(not ReplayStateStore._action_digest_matches(action) for action in payload.actions):
            raise ReplayStateIntegrityError("Replay action digest validation failed")

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @staticmethod
    def _normalize_run_id(value: UUID | str) -> UUID:
        try:
            parsed = UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise ReplayStateNotFoundError("Replay run identifier is invalid") from None
        if parsed.version != 4:
            raise ReplayStateNotFoundError("Replay run identifier is invalid")
        return parsed

    def _database_path(self, value: UUID | str) -> Path:
        replay_run_id = self._normalize_run_id(value)
        path = self.directory / f"{replay_run_id}.sqlite3"
        if path.is_symlink():
            raise ReplayStateIntegrityError("Replay state record is invalid")
        return path

    def _lock_path(self, value: UUID | str) -> Path:
        replay_run_id = self._normalize_run_id(value)
        path = self.directory / f"{replay_run_id}.lock"
        if path.is_symlink():
            raise ReplayStateIntegrityError("Replay state lock is invalid")
        return path

    def _acquire_run_lock(self, value: UUID | str) -> _InterProcessLock:
        lock = _InterProcessLock(self._lock_path(value))
        lock.acquire()
        return lock

    def _prepare_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            absolute = Path(os.path.abspath(self.directory))
            resolved = self.directory.resolve(strict=True)
            if absolute != resolved or not resolved.is_dir():
                raise OSError("Replay state directory is not a safe directory")
            os.chmod(resolved, 0o700)
            if os.name != "nt" and stat.S_IMODE(resolved.stat().st_mode) & 0o077:
                raise OSError("Replay state directory permissions are not private")
        except OSError:
            raise ReplayStateStoreError("Replay state storage directory is unavailable") from None

    @staticmethod
    def _set_private_file_mode(path: Path) -> None:
        os.chmod(path, 0o600)
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise OSError("Replay state file permissions are not private")

    @staticmethod
    def _configure_connection(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA busy_timeout=5000")

    @contextmanager
    def _write_state(self, replay_run_id: UUID | str) -> Iterator[_ReplayStatePayload]:
        path = self._database_path(replay_run_id)
        if not path.is_file():
            raise ReplayStateNotFoundError("Replay state record was not found")
        try:
            connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            self._configure_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            expected_run_id = self._normalize_run_id(replay_run_id)
            payload = self._read_payload(connection, expected_run_id)
            yield payload
            encoded = self.cipher.encrypt(payload.model_dump_json().encode("utf-8"))
            updated = connection.execute(
                f"UPDATE {self._TABLE} SET payload = ? WHERE singleton = 1",
                (encoded,),
            )
            if updated.rowcount != 1:
                raise ReplayStateIntegrityError("Replay state record is incomplete")
            connection.commit()
            self._set_private_file_mode(path)
        except ReplayStateStoreError:
            try:
                connection.rollback()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            raise
        except Exception:
            try:
                connection.rollback()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            raise ReplayStateStoreError("Replay state operation could not be completed safely") from None
        finally:
            try:
                connection.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass

    def _load(self, replay_run_id: UUID | str) -> _ReplayStatePayload:
        path = self._database_path(replay_run_id)
        if not path.is_file():
            raise ReplayStateNotFoundError("Replay state record was not found")
        try:
            connection = sqlite3.connect(path, timeout=5)
            try:
                self._configure_connection(connection)
                return self._read_payload(connection, self._normalize_run_id(replay_run_id))
            finally:
                connection.close()
        except ReplayStateStoreError:
            raise
        except Exception:
            raise ReplayStateIntegrityError("Replay state record is corrupted or incompatible") from None

    def _read_payload(
        self,
        connection: sqlite3.Connection,
        expected_run_id: UUID,
    ) -> _ReplayStatePayload:
        try:
            cursor = connection.execute(f"SELECT payload FROM {self._TABLE} WHERE singleton = 1")
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], (bytes, bytearray)):
                raise ReplayStateIntegrityError("Replay state record is incomplete")
            plaintext = self.cipher.decrypt(bytes(row[0]))
            payload = _ReplayStatePayload.model_validate_json(plaintext)
            self._validate_payload(payload)
            if payload.replay_run_id != expected_run_id:
                raise ReplayStateIntegrityError("Replay state record identity does not match its file")
            return payload
        except ReplayStateStoreError:
            raise
        except (ReplayStateEncryptionError, ValidationError, ValueError, TypeError, sqlite3.Error):
            raise ReplayStateIntegrityError("Replay state record failed integrity validation") from None

    @staticmethod
    def _validate_payload(payload: _ReplayStatePayload) -> None:
        if (
            payload.schema_version != 1
            or payload.checkpoint_version < 0
            or payload.next_action_index > len(payload.actions)
            or not ReplayStateStore._is_digest(payload.workflow_digest)
            or not payload.actions
        ):
            raise ReplayStateIntegrityError("Replay state record is incompatible")
        try:
            workflow_salt = base64.urlsafe_b64decode(
                payload.workflow_digest_salt + "=" * (-len(payload.workflow_digest_salt) % 4)
            )
        except (ValueError, TypeError):
            raise ReplayStateIntegrityError("Replay workflow digest salt is invalid") from None
        if (
            len(workflow_salt) != 32
            or base64.urlsafe_b64encode(workflow_salt).decode("ascii").rstrip("=")
            != payload.workflow_digest_salt
        ):
            raise ReplayStateIntegrityError("Replay workflow digest salt is invalid")
        step_ids = [action.step_id for action in payload.actions]
        if len(set(step_ids)) != len(step_ids) or len(payload.completed_step_ids) != payload.next_action_index:
            raise ReplayStateIntegrityError("Replay state action journal is invalid")
        if payload.completed_step_ids != step_ids[:payload.next_action_index]:
            raise ReplayStateIntegrityError("Replay state completed-step checkpoint is invalid")
        unknown_actions: list[str] = []
        in_progress_count = 0
        for index, action in enumerate(payload.actions):
            if not action.step_id.strip() or not action.action.strip():
                raise ReplayStateIntegrityError("Replay state action journal is invalid")
            if not ReplayStateStore._action_digest_matches(action):
                raise ReplayStateIntegrityError("Replay action digest validation failed")
            if index < payload.next_action_index:
                if action.state != ReplayActionJournalState.COMPLETED:
                    raise ReplayStateIntegrityError("Replay state completed action journal is invalid")
            elif index == payload.next_action_index:
                if action.state == ReplayActionJournalState.COMPLETED:
                    raise ReplayStateIntegrityError("Replay state cursor skips an incomplete action")
            elif action.state != ReplayActionJournalState.NOT_STARTED:
                raise ReplayStateIntegrityError("Replay state contains an out-of-order action")

            if action.state == ReplayActionJournalState.IN_PROGRESS:
                in_progress_count += 1
                if index != payload.next_action_index or action.started_at is None or action.completed_at is not None:
                    raise ReplayStateIntegrityError("Replay in-progress journal entry is invalid")
            elif action.state == ReplayActionJournalState.COMPLETED:
                if action.started_at is None or action.completed_at is None or action.result is None:
                    raise ReplayStateIntegrityError("Replay completed journal entry is incomplete")
            elif action.state == ReplayActionJournalState.OUTCOME_UNKNOWN:
                unknown_actions.append(action.step_id)
                if index != payload.next_action_index or action.started_at is None or action.completed_at is not None:
                    raise ReplayStateIntegrityError("Replay uncertain journal entry is invalid")
            elif action.state == ReplayActionJournalState.NOT_STARTED:
                if action.started_at is not None or action.completed_at is not None or action.result is not None:
                    raise ReplayStateIntegrityError("Replay not-started journal entry is invalid")

        if in_progress_count > 1:
            raise ReplayStateIntegrityError("Replay state has multiple in-progress actions")
        if unknown_actions != ([payload.uncertain_step_id] if payload.uncertain_step_id else []):
            raise ReplayStateIntegrityError("Replay uncertain-step checkpoint is invalid")
        if unknown_actions and not payload.needs_attention:
            raise ReplayStateIntegrityError("Uncertain replay action must require attention")
        if len(set(payload.consumed_authorization_ids)) != len(payload.consumed_authorization_ids):
            raise ReplayStateIntegrityError("Replay authorization journal is invalid")
        hashes = payload.consumed_authorization_hashes
        if len(set(hashes)) != len(hashes) or any(not ReplayStateStore._is_digest(item) for item in hashes):
            raise ReplayStateIntegrityError("Replay authorization journal is invalid")

    @staticmethod
    def _is_digest(value: str) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _assert_current(
        payload: _ReplayStatePayload,
        continuation: ReplayContinuation,
        tool_call: ToolCall,
    ) -> None:
        ReplayStateStore._assert_continuation(payload, continuation)
        if (
            tool_call.task_id != payload.task_id
            or tool_call.tool_call_id != payload.tool_call_id
            or tool_call.tool_name != "browser.replay"
        ):
            raise ReplayStateConflictError("Replay continuation does not match this tool call")

    @staticmethod
    def _assert_continuation(
        payload: _ReplayStatePayload,
        continuation: ReplayContinuation,
    ) -> None:
        expected = ReplayStateStore._continuation(payload)
        if continuation != expected:
            raise ReplayStateConflictError("Replay continuation is stale or does not match stored state")

    @staticmethod
    def _current_action(payload: _ReplayStatePayload) -> _StoredAction:
        if payload.next_action_index >= len(payload.actions):
            raise ReplayStateConflictError("Replay has no pending action")
        return payload.actions[payload.next_action_index]


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time comparison for fixed-format action digests."""
    import hmac
    return hmac.compare_digest(left, right)
