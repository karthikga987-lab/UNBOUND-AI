from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import shutil
from uuid import uuid4

import pytest

from backend.app.browser.replay_state import (
    PreparedReplayAction,
    ReplayActionJournalState,
    ReplayStepAuthorization,
)
from backend.app.browser.replay_state_store import (
    ReplayAuthorizationError,
    ReplayStateBusyError,
    ReplayStateConflictError,
    ReplayStateIntegrityError,
    ReplayStateNotFoundError,
    ReplayStateStore,
    ReplayWorkflowMismatchError,
)
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV, BrowserReplayStateCipher
from backend.app.schemas.contracts import ToolCall, WorkflowAction, WorkflowDefinition, WorkflowParameter


def _key_setting(key_id="test-v1", key=None):
    material = key or os.urandom(32)
    encoded = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    return f"{key_id}:{encoded}"


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting())
    workflow = WorkflowDefinition(
        name="State test workflow",
        start_url="https://example.test/start",
        parameters=[WorkflowParameter(name="password", type="password", required=True)],
        steps=[
            WorkflowAction(
                step_id="fill-secret",
                action="fill",
                target={"type": "label", "value": "Password"},
                input={"value": "{{password}}"},
            ),
            WorkflowAction(
                step_id="submit",
                action="click",
                target={"type": "role", "role": "button", "name": "Submit"},
            ),
        ],
    )
    tool_call = ToolCall(
        task_id=uuid4(),
        step_id="replay-call",
        tool_name="browser.replay",
        arguments={"workflow_id": str(workflow.workflow_id)},
    )
    session_id = uuid4()
    secret = "sensitive-replay-secret-92"
    trusted_token = "opaque-orchestrator-test-token"

    def trusted_authorization(auth, call):
        return call.task_id == auth.task_id and auth.one_time_token.get_secret_value() == trusted_token

    store = ReplayStateStore(
        tmp_path / "replay-state",
        cipher=BrowserReplayStateCipher(),
        authorization_validator=trusted_authorization,
        session_validator=lambda task_id, active_session_id: (
            task_id == tool_call.task_id and active_session_id == session_id
        ),
    )
    result = store.create(
        workflow,
        tool_call=tool_call,
        session_id=session_id,
        resolved_parameters={"password": secret},
        prepared_actions=[
            PreparedReplayAction(
                step_id="fill-secret",
                action="fill",
                arguments={"target": {"type": "label", "value": "Password"}, "value": secret},
            ),
            PreparedReplayAction(
                step_id="submit",
                action="click",
                arguments={"target": {"type": "role", "role": "button", "name": "Submit"}},
            ),
        ],
    )
    return {
        "directory": tmp_path / "replay-state",
        "store": store,
        "workflow": workflow,
        "tool_call": tool_call,
        "session_id": session_id,
        "secret": secret,
        "token": trusted_token,
        "result": result,
    }


def _context(env):
    return env["store"].get_pending_policy_context(
        env["result"].continuation,
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
    )


def _authorization(context, token, *, authorization_id=None, now=None, action_digest=None):
    now = now or datetime.now(timezone.utc)
    return ReplayStepAuthorization(
        authorization_id=authorization_id or uuid4(),
        task_id=context.task_id,
        tool_call_id=context.tool_call_id,
        replay_run_id=context.replay_run_id,
        continuation_id=context.continuation_id,
        workflow_id=context.workflow_id,
        step_id=context.step_id,
        action_digest=action_digest or context.action_digest,
        decision="allowed_without_approval",
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        one_time_token=token,
    )


def _claim(env, *, authorization=None, continuation=None, active_session_id=None, now=None):
    context = _context(env)
    return env["store"].claim_action(
        continuation or env["result"].continuation,
        authorization or _authorization(context, env["token"], now=now),
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
        active_session_id=active_session_id or env["session_id"],
        now=now,
    )


def test_encrypted_state_persists_and_reloads_without_sensitive_plaintext(state_env):
    env = state_env
    restored = ReplayStateStore(
        env["directory"],
        cipher=BrowserReplayStateCipher(),
        authorization_validator=env["store"].authorization_validator,
        session_validator=env["store"].session_validator,
    )
    start_context = restored.get_pending_policy_context(
        env["result"].continuation,
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
    )
    start_lease = restored.claim_action(
        env["result"].continuation,
        _authorization(start_context, env["token"]),
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
        active_session_id=env["session_id"],
    )
    result = restored.complete_action(start_lease, result={"loaded": True})
    db_path = env["directory"] / f"{env['result'].replay_run_id}.sqlite3"
    raw_db = db_path.read_bytes()
    result = restored.load_result(
        env["result"].replay_run_id,
        current_workflow=env["workflow"],
    )
    context = restored.get_pending_policy_context(
        result.continuation,
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
    )

    assert env["secret"].encode() not in raw_db
    assert context.exact_arguments["value"] == env["secret"]
    assert env["secret"] not in repr(context)
    assert env["secret"] not in context.model_dump_json()
    assert result.status == "pending_action"
    assert result.pending_action.step_id == "fill-secret"


def test_state_can_be_loaded_after_a_process_restart(state_env):
    env = state_env
    script = (
        "from backend.app.browser.replay_state_store import ReplayStateStore; "
        "from backend.app.browser.state_encryption import BrowserReplayStateCipher; "
        "from backend.app.schemas.contracts import WorkflowDefinition; "
        "from pathlib import Path; import json; "
        "store=ReplayStateStore(Path(r'" + str(env["directory"]) + "'), cipher=BrowserReplayStateCipher()); "
        "result=store.load_result('" + str(env["result"].replay_run_id) + "', "
        "current_workflow=WorkflowDefinition.model_validate_json(Path(r'"
        + str(env["directory"].parent / "workflow.json")
        + "').read_text())); print(result.status)"
    )
    workflow_path = env["directory"].parent / "workflow.json"
    workflow_path.write_text(env["workflow"].model_dump_json(), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "pending_action"
    workflow_path.unlink()


def test_corrupted_ciphertext_and_invalid_record_fail_safely(state_env):
    env = state_env
    db_path = env["directory"] / f"{env['result'].replay_run_id}.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE replay_state SET payload = ? WHERE singleton = 1", (b"tampered",))

    with pytest.raises(ReplayStateIntegrityError, match="failed integrity validation") as error:
        env["store"].load_result(env["result"].replay_run_id, current_workflow=env["workflow"])
    assert env["secret"] not in str(error.value)

    with pytest.raises(ReplayStateNotFoundError, match="invalid"):
        env["store"].load_result("../../outside", current_workflow=env["workflow"])


def test_incomplete_encrypted_record_fails_closed(state_env):
    env = state_env
    db_path = env["directory"] / f"{env['result'].replay_run_id}.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM replay_state WHERE singleton = 1")

    with pytest.raises(ReplayStateIntegrityError, match="incomplete"):
        env["store"].load_result(env["result"].replay_run_id, current_workflow=env["workflow"])


def test_action_arguments_tampering_breaks_the_stored_digest(state_env):
    env = state_env
    db_path = env["directory"] / f"{env['result'].replay_run_id}.sqlite3"
    with sqlite3.connect(db_path) as connection:
        encrypted = connection.execute("SELECT payload FROM replay_state WHERE singleton = 1").fetchone()[0]
        state = json.loads(env["store"].cipher.decrypt(encrypted))
        state["actions"][1]["arguments"]["value"] = "changed-argument-value"
        connection.execute(
            "UPDATE replay_state SET payload = ? WHERE singleton = 1",
            (env["store"].cipher.encrypt(json.dumps(state).encode()),),
        )

    with pytest.raises(ReplayStateIntegrityError, match="digest validation failed"):
        env["store"].load_result(env["result"].replay_run_id, current_workflow=env["workflow"])


def test_state_file_cannot_be_relocated_under_another_uuid(state_env, tmp_path):
    env = state_env
    source = env["directory"] / f"{env['result'].replay_run_id}.sqlite3"
    other_run_id = uuid4()
    shutil.copyfile(source, env["directory"] / f"{other_run_id}.sqlite3")

    with pytest.raises(ReplayStateIntegrityError, match="identity"):
        env["store"].load_result(other_run_id, current_workflow=env["workflow"])


def test_missing_encryption_key_prevents_store_construction(state_env, monkeypatch):
    monkeypatch.delenv(ACTIVE_KEY_ENV, raising=False)
    from backend.app.browser.state_encryption import ReplayStateEncryptionConfigurationError

    with pytest.raises(ReplayStateEncryptionConfigurationError):
        ReplayStateStore(state_env["directory"] / "missing", authorization_validator=lambda *_: True)


def test_wrong_key_cannot_load_replay_state(state_env, monkeypatch):
    env = state_env
    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting("test-v1", b"w" * 32))
    wrong_store = ReplayStateStore(env["directory"], authorization_validator=lambda *_: True)

    with pytest.raises(ReplayStateIntegrityError):
        wrong_store.load_result(env["result"].replay_run_id, current_workflow=env["workflow"])


def test_authorization_is_bound_to_exact_action_digest(state_env):
    env = state_env
    context = _context(env)
    authorization = _authorization(
        context,
        env["token"],
        action_digest="0" * 64,
    )

    with pytest.raises(ReplayAuthorizationError, match="does not match"):
        _claim(env, authorization=authorization)
    assert _context(env).step_id == "workflow.start"


def test_expired_authorization_is_rejected(state_env):
    env = state_env
    now = datetime.now(timezone.utc)
    authorization = _authorization(_context(env), env["token"], now=now - timedelta(hours=1))

    with pytest.raises(ReplayAuthorizationError, match="expired"):
        _claim(env, authorization=authorization, now=now)


def test_authorization_is_consumed_once_and_completed_steps_do_not_repeat(state_env):
    env = state_env
    context = _context(env)
    authorization_id = uuid4()
    authorization = _authorization(context, env["token"], authorization_id=authorization_id)
    lease = _claim(env, authorization=authorization)
    result = env["store"].complete_action(lease, result={"loaded": True})

    assert result.continuation.completed_step_ids == ("workflow.start",)
    assert result.continuation.next_action_index == 1
    assert result.checkpoints[0].state == ReplayActionJournalState.COMPLETED
    assert result.pending_action.step_id == "fill-secret"
    assert env["store"].get_action_outcome(result.replay_run_id, "workflow.start").result == {"loaded": True}
    with pytest.raises(ReplayStateConflictError, match="stale"):
        _claim(env, authorization=authorization, continuation=env["result"].continuation)

    next_context = env["store"].get_pending_policy_context(
        result.continuation,
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
    )
    reused_token = _authorization(next_context, env["token"])
    with pytest.raises(ReplayAuthorizationError, match="already been consumed"):
        env["store"].claim_action(
            result.continuation,
            reused_token,
            tool_call=env["tool_call"],
            current_workflow=env["workflow"],
            active_session_id=env["session_id"],
        )


def test_concurrent_resume_attempts_cannot_claim_the_same_checkpoint(state_env):
    env = state_env
    context = _context(env)
    authorization = _authorization(context, env["token"])

    def attempt():
        return _claim(env, authorization=authorization)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except (ReplayStateBusyError, ReplayStateConflictError) as exc:
                outcomes.append(exc)

    leases = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(leases) == 1
    assert len(failures) == 1
    leases[0].release()


def test_restart_recovery_marks_in_progress_action_unknown_and_needs_attention(state_env):
    env = state_env
    lease = _claim(env)
    lease.release()  # models process death after the durable in_progress claim

    recovered = env["store"].recover_after_restart(env["result"].replay_run_id)

    assert recovered.status == "needs_attention"
    assert recovered.continuation.uncertain_step_id == "workflow.start"
    assert recovered.checkpoints[0].state == ReplayActionJournalState.OUTCOME_UNKNOWN
    with pytest.raises(ReplayStateConflictError, match="attention"):
        env["store"].get_pending_policy_context(
            recovered.continuation,
            tool_call=env["tool_call"],
            current_workflow=env["workflow"],
        )


def test_actual_process_crash_after_claim_recovers_as_outcome_unknown(state_env):
    env = state_env
    context = _context(env)
    authorization = _authorization(context, env["token"])
    request_file = env["directory"].parent / "claim-request.json"
    authorization_data = authorization.model_dump(mode="json")
    authorization_data["one_time_token"] = authorization.one_time_token.get_secret_value()
    request_file.write_text(json.dumps({
        "directory": str(env["directory"]),
        "continuation": env["result"].continuation.model_dump(mode="json"),
        "authorization": authorization_data,
        "tool_call": env["tool_call"].model_dump(mode="json"),
        "workflow": env["workflow"].model_dump(mode="json"),
        "session_id": str(env["session_id"]),
    }), encoding="utf-8")
    script = r"""
import json, os, sys
from pathlib import Path
from uuid import UUID
from backend.app.browser.replay_state import ReplayContinuation, ReplayStepAuthorization
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.schemas.contracts import ToolCall, WorkflowDefinition
data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
store = ReplayStateStore(
    data['directory'],
    authorization_validator=lambda *_: True,
    session_validator=lambda task, session: str(session) == data['session_id'],
)
lease = store.claim_action(
    ReplayContinuation.model_validate(data['continuation']),
    ReplayStepAuthorization.model_validate(data['authorization']),
    tool_call=ToolCall.model_validate(data['tool_call']),
    current_workflow=WorkflowDefinition.model_validate(data['workflow']),
    active_session_id=UUID(data['session_id']),
)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(request_file)],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    recovered = env["store"].recover_after_restart(env["result"].replay_run_id)
    assert recovered.status == "needs_attention"
    assert recovered.checkpoints[0].state == ReplayActionJournalState.OUTCOME_UNKNOWN
    request_file.unlink()


def test_uncertain_action_is_not_retried(state_env):
    env = state_env
    lease = _claim(env)
    result = env["store"].mark_action_outcome_unknown(lease)

    assert result.status == "needs_attention"
    assert result.continuation.uncertain_step_id == "workflow.start"
    with pytest.raises(ReplayStateConflictError, match="attention"):
        env["store"].get_pending_policy_context(
            result.continuation,
            tool_call=env["tool_call"],
            current_workflow=env["workflow"],
        )


def test_browser_session_loss_returns_needs_attention(state_env):
    env = state_env
    result = env["store"].mark_session_lost(env["result"].continuation)

    assert result.status == "needs_attention"
    assert "continuity" in result.error
    with pytest.raises(ReplayStateConflictError, match="attention"):
        env["store"].get_pending_policy_context(
            result.continuation,
            tool_call=env["tool_call"],
            current_workflow=env["workflow"],
        )


def test_missing_session_validator_fails_closed(state_env):
    env = state_env
    unconfigured = ReplayStateStore(
        env["directory"],
        cipher=BrowserReplayStateCipher(),
        authorization_validator=env["store"].authorization_validator,
    )
    continuation = env["result"].continuation
    context = unconfigured.get_pending_policy_context(
        continuation,
        tool_call=env["tool_call"],
        current_workflow=env["workflow"],
    )

    with pytest.raises(ReplayStateConflictError, match="session continuity"):
        unconfigured.claim_action(
            continuation,
            _authorization(context, env["token"]),
            tool_call=env["tool_call"],
            current_workflow=env["workflow"],
            active_session_id=env["session_id"],
        )
    status = env["store"].load_result(
        continuation.replay_run_id,
        current_workflow=env["workflow"],
    )
    assert status.status == "needs_attention"


def test_workflow_digest_mismatch_requires_attention(state_env):
    env = state_env
    changed = env["workflow"].model_copy(update={"description": "changed after replay was prepared"})

    result = env["store"].load_result(env["result"].replay_run_id, current_workflow=changed)

    assert result.status == "needs_attention"
    assert "version changed" in result.error
    with pytest.raises(ReplayWorkflowMismatchError):
        env["store"].get_pending_policy_context(
            result.continuation,
            tool_call=env["tool_call"],
            current_workflow=changed,
        )


def test_authorization_secret_is_not_in_public_model_serialization(state_env):
    env = state_env
    authorization = _authorization(_context(env), env["token"])

    assert env["token"] not in repr(authorization)
    assert env["token"] not in authorization.model_dump_json()
    assert env["secret"] not in env["result"].model_dump_json()
