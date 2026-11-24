from __future__ import annotations

import json

import pytest

from backend.app.browser import BrowserController
from backend.app.browser import BrowserRecorder
from backend.app.schemas.contracts import WorkflowAction, WorkflowDefinition


class FakeLocator:
    def __init__(self, attributes=None):
        self.attributes = attributes or {}
        self.calls = []

    def get_attribute(self, name):
        return self.attributes.get(name)

    def inner_text(self, **kwargs):
        return self.attributes.get("text", "")

    def click(self, **kwargs):
        self.calls.append(("click", kwargs))

    def fill(self, value, **kwargs):
        self.calls.append(("fill", value, kwargs))

    def select_option(self, **kwargs):
        self.calls.append(("select_option", kwargs))


class FakePage:
    def __init__(self):
        self.url = "https://example.test/home"
        self.targets = []

    def goto(self, url, **kwargs):
        self.url = url

    def get_by_role(self, role, **kwargs):
        locator = FakeLocator({"role": role, "aria-label": kwargs.get("name"), "text": kwargs.get("name") or ""})
        self.targets.append(locator)
        return locator

    def get_by_label(self, value, **kwargs):
        attributes = {"aria-label": value, "name": "email" if "email" in value.lower() else "password"}
        locator = FakeLocator(attributes)
        self.targets.append(locator)
        return locator

    def get_by_placeholder(self, value, **kwargs):
        locator = FakeLocator({"placeholder": value})
        self.targets.append(locator)
        return locator

    def get_by_text(self, value, **kwargs):
        locator = FakeLocator({"text": value})
        self.targets.append(locator)
        return locator

    def get_by_test_id(self, value):
        locator = FakeLocator({"id": value})
        self.targets.append(locator)
        return locator

    def locator(self, value):
        if value == "body":
            return FakeLocator()
        locator = FakeLocator({"id": value.lstrip("#")})
        self.targets.append(locator)
        return locator


@pytest.fixture
def recorder():
    controller = BrowserController(timeout_ms=1000)
    controller._page = FakePage()
    return BrowserRecorder(controller), controller


def test_records_navigation_click_fills_and_selection_in_order(recorder):
    browser_recorder, _ = recorder
    browser_recorder.start_recording("Quarterly report", description="Get a report")
    browser_recorder.navigate("https://example.test/reports?year=2025#section")
    browser_recorder.click({"type": "role", "role": "button", "name": "Search"})
    browser_recorder.fill({"type": "label", "value": "Email address"}, "user@example.test")
    browser_recorder.select_option({"type": "label", "value": "Report year"}, "2025")
    workflow = browser_recorder.stop_recording()

    assert isinstance(workflow, WorkflowDefinition)
    assert all(isinstance(step, WorkflowAction) for step in workflow.steps)
    assert [step.step_id for step in workflow.steps] == ["step_1", "step_2", "step_3", "step_4"]
    assert [step.action for step in workflow.steps] == ["navigate", "click", "fill", "select_option"]
    assert workflow.steps[1].target["role"] == "button"
    assert workflow.steps[1].target["name"] == "Search"
    assert workflow.steps[2].input == {"value": "{{email_address}}"}
    assert workflow.steps[3].input == {"value": "{{report_year}}"}
    assert workflow.steps[2].target["type"] == "label"
    assert workflow.steps[0].input["url"] == "https://example.test/reports?year=2025"
    assert [parameter.name for parameter in workflow.parameters] == ["email_address", "report_year"]
    restored = WorkflowDefinition.model_validate_json(workflow.model_dump_json())
    assert restored.steps == workflow.steps
    assert restored.parameters == workflow.parameters


def test_sensitive_values_are_parameterized_and_never_serialized(recorder):
    browser_recorder, _ = recorder
    browser_recorder.start_recording("Login flow")
    browser_recorder.fill({"type": "label", "value": "Password"}, "TOP-SECRET-DO-NOT-STORE")
    workflow = browser_recorder.stop_recording()
    encoded = workflow.model_dump_json()

    assert "TOP-SECRET-DO-NOT-STORE" not in encoded
    assert workflow.steps[0].input == {"value": "{{password}}"}
    assert "Sensitive value" in workflow.parameters[0].description
    assert workflow.parameters[0].required is True
    assert workflow.parameters[0].default is None


def test_sensitive_url_query_and_fragment_are_removed(recorder):
    browser_recorder, _ = recorder
    browser_recorder.start_recording("Secure page", start_url="https://example.test/?access_token=SECRET#private")
    workflow = browser_recorder.stop_recording()
    serialized = json.dumps(workflow.model_dump(mode="json"))
    assert "SECRET" not in serialized
    assert "private" not in serialized
    assert "access_token=" in str(workflow.start_url)


def test_step_ids_restart_for_each_recording_and_no_browser_activity_is_allowed_outside_it(recorder):
    browser_recorder, _ = recorder
    with pytest.raises(RuntimeError, match="Start a workflow recording"):
        browser_recorder.navigate("https://example.test")
    browser_recorder.start_recording("First")
    browser_recorder.click({"type": "role", "role": "link", "name": "Home"})
    assert browser_recorder.stop_recording().steps[0].step_id == "step_1"
    browser_recorder.start_recording("Second")
    browser_recorder.click({"type": "role", "role": "link", "name": "Home"})
    assert browser_recorder.stop_recording().steps[0].step_id == "step_1"


def test_recorder_rejects_start_without_started_controller():
    with pytest.raises(RuntimeError, match="must be started"):
        BrowserRecorder(BrowserController()).start_recording("Not ready")


def test_target_capture_never_reads_or_records_input_value(recorder):
    browser_recorder, controller = recorder
    browser_recorder.start_recording("Email")
    browser_recorder.fill({"type": "css", "selector": "#email"}, "VALUE-THAT-MUST-NOT-APPEAR")
    workflow = browser_recorder.stop_recording()
    assert "VALUE-THAT-MUST-NOT-APPEAR" not in workflow.model_dump_json()
    assert controller._page.targets[0].calls == []  # target inspection did not trigger page actions
