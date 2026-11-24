from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.browser import BrowserController, WorkflowReplay, WorkflowStore
from backend.app.schemas.contracts import WorkflowAction, WorkflowDefinition, WorkflowParameter


class FakeLocator:
    def __init__(self, page, strategy, value, name=None):
        self.page = page
        self.strategy = strategy
        self.value = value
        self.name = name

    def count(self):
        return self.page.available.get((self.strategy, self.value), 1)

    def click(self, **kwargs):
        self.page._act("click", self.strategy, self.value)

    def fill(self, value, **kwargs):
        self.page._act("fill", self.strategy, self.value, value)

    def select_option(self, *, value, **kwargs):
        self.page._act("select_option", self.strategy, self.value, value)


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.available = {}
        self.actions = []
        self.fail_on = None

    def goto(self, url, **kwargs):
        self.url = url
        self.actions.append(("navigate", url))

    def get_by_role(self, role, *, name=None, **kwargs):
        return FakeLocator(self, "role", role, name)

    def get_by_label(self, value, **kwargs):
        return FakeLocator(self, "label", value)

    def get_by_placeholder(self, value, **kwargs):
        return FakeLocator(self, "placeholder", value)

    def get_by_text(self, value, **kwargs):
        return FakeLocator(self, "text", value)

    def get_by_test_id(self, value):
        return FakeLocator(self, "test_id", value)

    def locator(self, value):
        return FakeLocator(self, "css", value)

    def _act(self, action, strategy, target, value=None):
        if self.fail_on == action:
            suffix = f" {value}" if value is not None else ""
            raise RuntimeError(f"mock {action} failed{suffix}")
        self.actions.append((action, strategy, target, value))


@pytest.fixture
def replay_env(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    controller = BrowserController(timeout_ms=100)
    page = FakePage()
    controller._page = page
    return WorkflowReplay(store, controller), store, page


def make_workflow(steps, *, parameters=None, start_url="https://example.test/start"):
    return WorkflowDefinition(
        name="Replay test",
        start_url=start_url,
        parameters=parameters or [],
        steps=steps,
    )


def test_replay_executes_actions_in_order_with_parameter_substitution(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow(
        [
            WorkflowAction(step_id="go", action="navigate", input={"url": "{{destination}}"}),
            WorkflowAction(
                step_id="search", action="click",
                target={"type": "role", "role": "button", "name": "Search"},
            ),
            WorkflowAction(
                step_id="email", action="fill",
                target={"type": "label", "value": "Email"},
                input={"value": "{{email}}"},
            ),
            WorkflowAction(
                step_id="year", action="select_option",
                target={"type": "label", "value": "Year"},
                input={"value": "{{year}}"},
            ),
        ],
        parameters=[
            WorkflowParameter(name="destination", type="url", required=True),
            WorkflowParameter(name="email", type="string", required=True),
            WorkflowParameter(name="year", type="integer", required=True),
        ],
    )
    store.save(workflow)

    pending = replay.replay(
        str(workflow.workflow_id),
        {"destination": "https://example.test/results", "email": "person@example.test", "year": 2025},
    )

    # The current safety policy treats an action targeting an email field as
    # sensitive. Replay must pause until that exact fill step is approved.
    assert pending.status == "awaiting_approval"
    assert [step.step_id for step in pending.steps] == ["email"]
    assert pending.steps[0].awaiting_approval is True
    assert page.actions == []

    result = replay.replay(
        str(workflow.workflow_id),
        {"destination": "https://example.test/results", "email": "person@example.test", "year": 2025},
        approved_step_ids={"email"},
    )

    assert result.success is True
    assert result.status == "completed"
    assert [step.step_id for step in result.steps] == ["workflow.start", "go", "search", "email", "year"]
    assert page.actions == [
        ("navigate", "https://example.test/start"),
        ("navigate", "https://example.test/results"),
        ("click", "role", "button", None),
        ("fill", "label", "Email", "person@example.test"),
        ("select_option", "label", "Year", "2025"),
    ]


def test_missing_required_parameter_prevents_all_browser_actions(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow(
        [WorkflowAction(step_id="fill", action="fill", target={"type": "label", "value": "Name"}, input={"value": "{{name}}"})],
        parameters=[WorkflowParameter(name="name", type="string", required=True)],
    )
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id))

    assert result.status == "failed"
    assert "name" in result.error
    assert page.actions == []


def test_invalid_and_unknown_parameters_are_rejected_before_execution(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow(
        [WorkflowAction(step_id="go", action="navigate", input={"url": "https://example.test"})],
        parameters=[WorkflowParameter(name="year", type="integer", required=True)],
    )
    store.save(workflow)

    invalid = replay.replay(str(workflow.workflow_id), {"year": "not-an-integer"})
    unknown = replay.replay(str(workflow.workflow_id), {"year": 2025, "extra": "ignored?"})

    assert "expected integer" in invalid.error
    assert "Unknown workflow parameter" in unknown.error
    assert page.actions == []


def test_missing_locator_fails_step_and_stops_later_actions(replay_env):
    replay, store, page = replay_env
    page.available[("css", "#missing")] = 0
    workflow = make_workflow([
        WorkflowAction(step_id="missing", action="click", target={"type": "css", "selector": "#missing"}),
        # A clearly read-only target keeps this regression focused on the
        # missing first locator rather than adding another approval gate.
        WorkflowAction(step_id="later", action="click", target={"type": "role", "role": "button", "name": "Search"}),
    ])
    store.save(workflow)

    pending = replay.replay(str(workflow.workflow_id))
    # Opaque CSS clicks require approval under the existing fail-closed policy.
    assert pending.status == "awaiting_approval"
    assert pending.steps[-1].step_id == "missing"
    assert pending.steps[-1].awaiting_approval is True
    assert page.actions == []

    # Once the exact step is approved, replay can inspect its locator and report
    # that it is missing without attempting the click or later workflow steps.
    result = replay.replay(str(workflow.workflow_id), approved_step_ids={"missing"})

    assert result.status == "failed"
    assert [step.step_id for step in result.steps] == ["workflow.start", "missing"]
    assert "No matching element" in result.steps[-1].error
    assert page.actions == [("navigate", "https://example.test/start")]


def test_missing_primary_locator_uses_one_semantic_fallback(replay_env):
    replay, store, page = replay_env
    page.available[("css", "#old-search")] = 0
    page.available[("text", "Continue")] = 1
    workflow = make_workflow([
        WorkflowAction(
            step_id="continue", action="click",
            target={"type": "css", "selector": "#old-search", "text": "Continue"},
        ),
    ])
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id))

    assert result.success is True
    assert result.steps[-1].fallback_used is True
    assert page.actions[-1] == ("click", "text", "Continue", None)


def test_failed_action_preserves_safe_error_and_stops_workflow(replay_env):
    replay, store, page = replay_env
    page.fail_on = "click"
    workflow = make_workflow([
        WorkflowAction(step_id="click", action="click", target={"type": "role", "role": "button", "name": "Search"}),
        WorkflowAction(step_id="after", action="navigate", input={"url": "https://example.test/after"}),
    ])
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id))

    assert result.status == "failed"
    assert "mock click failed" in result.steps[-1].error
    assert [step.step_id for step in result.steps] == ["workflow.start", "click"]
    assert len(page.actions) == 1


def test_sensitive_parameter_never_appears_in_replay_results_or_errors(replay_env):
    replay, store, page = replay_env
    secret = "VERY-PRIVATE-PASSWORD-777"
    page.fail_on = "fill"
    workflow = make_workflow(
        [WorkflowAction(
            step_id="password", action="fill",
            target={"type": "label", "value": "Password"},
            input={"value": "{{password}}"},
        )],
        parameters=[WorkflowParameter(name="password", type="password", required=True)],
    )
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id), {"password": secret}, approved_step_ids={"password"})
    rendered = repr(result)

    assert result.status == "failed"
    assert "[REDACTED]" in result.error
    assert secret not in rendered


def test_consequential_click_waits_for_explicit_step_approval(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow([
        WorkflowAction(
            step_id="submit", action="click",
            target={"type": "role", "role": "button", "name": "Submit payment"},
        ),
    ])
    store.save(workflow)

    pending = replay.replay(str(workflow.workflow_id))
    assert pending.status == "awaiting_approval"
    assert pending.steps[-1].awaiting_approval is True
    assert page.actions == []

    approved = replay.replay(str(workflow.workflow_id), approved_step_ids={"submit"})
    assert approved.success is True
    assert page.actions[-1] == ("click", "role", "button", None)


def test_unsupported_action_is_rejected_before_browser_activity(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow([WorkflowAction(step_id="custom", action="run_script")])
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id))

    assert result.status == "failed"
    assert "unsupported action" in result.error
    assert page.actions == []


def test_duplicate_step_ids_are_rejected_before_browser_activity(replay_env):
    replay, store, page = replay_env
    workflow = make_workflow([
        WorkflowAction(step_id="same", action="navigate", input={"url": "https://example.test/a"}),
        WorkflowAction(step_id="same", action="navigate", input={"url": "https://example.test/b"}),
    ])
    store.save(workflow)

    result = replay.replay(str(workflow.workflow_id))

    assert result.status == "failed"
    assert "duplicate step ID" in result.error
    assert page.actions == []
