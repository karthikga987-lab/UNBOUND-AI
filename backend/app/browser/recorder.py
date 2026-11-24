"""Record explicit browser-controller actions as shared workflow contracts.

The recorder observes operations routed through its methods. It does not inject
JavaScript or listen to page scripts, and it never persists the values passed to
form controls: values become required workflow parameters instead.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.app.browser.controller import BrowserController, LocatorTarget, _parse_target
from backend.app.schemas.contracts import WorkflowAction, WorkflowDefinition, WorkflowParameter


_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|passcode|one.?time|\botp\b|token|secret|auth|credential|"
    r"api.?key|private.?key|access.?key|client.?key|refresh.?key|bearer|jwt|signature|"
    r"cookie|session|cvv|cvc|card.?number|security.?code|social.?security|\bssn\b|"
    r"national.?id|tax.?id|bank.?account|routing.?number)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:password|passwd|token|secret|auth|credential|cookie|session|code|key|jwt|bearer|signature)",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-zA-Z0-9]+")


def _safe_url(url: str) -> str:
    """Remove URL fragments and known secret query values before persistence."""
    parts = urlsplit(url)
    safe_query = [
        (key, "" if _SENSITIVE_QUERY_KEY.search(key) else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


class BrowserRecorder:
    """Record ordered navigation and locator actions for one browser session."""

    def __init__(self, controller: BrowserController) -> None:
        self.controller = controller
        self._name: str | None = None
        self._description = ""
        self._start_url: str | None = None
        self._steps: list[WorkflowAction] = []
        self._parameters: list[WorkflowParameter] = []
        self._parameter_names: set[str] = set()

    @property
    def is_recording(self) -> bool:
        return self._name is not None

    def start_recording(
        self,
        name: str,
        *,
        description: str = "",
        start_url: str | None = None,
    ) -> None:
        if self.is_recording:
            raise RuntimeError("A workflow recording is already active")
        if not self.controller.is_started:
            raise RuntimeError("Browser session must be started before recording")
        current = start_url if start_url is not None else self.controller.current_url
        safe_start_url = _safe_url(current)
        # Validate early using the shared model rather than creating a parallel URL rule.
        WorkflowDefinition(name=name, description=description, start_url=safe_start_url)
        self._name = name
        self._description = description
        self._start_url = safe_start_url
        self._steps = []
        self._parameters = []
        self._parameter_names = set()

    def stop_recording(self) -> WorkflowDefinition:
        if not self.is_recording or self._start_url is None:
            raise RuntimeError("No workflow recording is active")
        definition = WorkflowDefinition(
            name=self._name,
            description=self._description,
            start_url=self._start_url,
            parameters=list(self._parameters),
            steps=list(self._steps),
        )
        self._name = None
        self._description = ""
        self._start_url = None
        self._steps = []
        self._parameters = []
        self._parameter_names = set()
        return definition

    def navigate(self, url: str) -> str:
        self._require_recording()
        final_url = self.controller.navigate(url)
        safe_url = _safe_url(final_url)
        self._append("navigate", input={"url": safe_url}, expected_outcome="The requested page loaded")
        return final_url

    def click(self, target: LocatorTarget | Mapping[str, Any]) -> None:
        self._require_recording()
        page = self.controller._require_page("record click")
        locator = self.controller._locator(page, target)
        captured_target, _ = self._capture_target(locator, target)
        self.controller.click(target)
        self._append("click", target=captured_target, expected_outcome="The target was activated")

    def fill(self, target: LocatorTarget | Mapping[str, Any], value: str) -> None:
        self._require_recording()
        page = self.controller._require_page("record form fill")
        locator = self.controller._locator(page, target)
        captured_target, metadata = self._capture_target(locator, target)
        # Never store the supplied value, including when the field looks ordinary.
        parameter_name = self._new_parameter_name(captured_target, metadata)
        secret = self._is_sensitive(metadata)
        description = (
            f"Sensitive value for {self._target_description(captured_target)}; provide securely at replay time"
            if secret else f"Value for {self._target_description(captured_target)}"
        )
        self.controller.fill(target, value)
        self._add_parameter(parameter_name, description)
        self._append(
            "fill",
            target=captured_target,
            input={"value": "{{" + parameter_name + "}}"},
            expected_outcome="The field contains the supplied parameter",
        )

    def select_option(
        self,
        target: LocatorTarget | Mapping[str, Any],
        value: str,
    ) -> None:
        """Select an option and record its value as a reusable parameter."""
        self._require_recording()
        page = self.controller._require_page("record selection")
        locator = self.controller._locator(page, target)
        captured_target, metadata = self._capture_target(locator, target)
        parameter_name = self._new_parameter_name(captured_target, metadata)
        secret = self._is_sensitive(metadata)
        description = (
            f"Sensitive selection for {self._target_description(captured_target)}; provide securely at replay time"
            if secret else f"Selection for {self._target_description(captured_target)}"
        )
        try:
            locator.select_option(value=value, timeout=self.controller.timeout_ms)
        except Exception as exc:
            from backend.app.browser.controller import BrowserControllerError
            raise BrowserControllerError("select option", str(exc), url=page.url) from exc
        self._add_parameter(parameter_name, description)
        self._append(
            "select_option",
            target=captured_target,
            input={"value": "{{" + parameter_name + "}}"},
            expected_outcome="The requested option was selected",
        )

    def _require_recording(self) -> None:
        if not self.is_recording:
            raise RuntimeError("Start a workflow recording before recording actions")

    def _append(
        self,
        action: str,
        *,
        target: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        expected_outcome: str | None = None,
    ) -> None:
        self._steps.append(WorkflowAction(
            step_id=f"step_{len(self._steps) + 1}",
            action=action,
            target=target,
            input=input or {},
            expected_outcome=expected_outcome,
        ))

    @staticmethod
    def _capture_target(locator, target: LocatorTarget | Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        parsed = _parse_target(target)
        metadata: dict[str, str] = {"target_value": parsed.value}
        result: dict[str, Any] = {"type": parsed.kind}
        if parsed.kind == "role":
            result["role"] = parsed.value
            metadata["target_role"] = parsed.value
            if parsed.name:
                result["name"] = parsed.name
                metadata["target_name"] = parsed.name
        else:
            result["value"] = parsed.value
        if parsed.exact:
            result["exact"] = True

        for attribute in ("id", "name", "aria-label", "placeholder", "type", "autocomplete", "href", "role"):
            try:
                value = locator.get_attribute(attribute)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                if attribute == "href":
                    value = _safe_url(value.strip())
                metadata[attribute] = value.strip()
                recorded_attribute = {
                    "aria-label": "aria_label",
                    "type": "input_type",
                    "role": "element_role",
                    "name": "element_name",
                }.get(attribute, attribute)
                result[recorded_attribute] = value.strip()
        try:
            text = locator.inner_text(timeout=500).strip()
        except Exception:
            text = ""
        if text and len(text) <= 120 and not _SENSITIVE_FIELD.search(text):
            result.setdefault("text", text)
            metadata["text"] = text
        return result, metadata

    @staticmethod
    def _is_sensitive(metadata: Mapping[str, str]) -> bool:
        return any(_SENSITIVE_FIELD.search(value) for value in metadata.values())

    def _new_parameter_name(self, target: Mapping[str, Any], metadata: Mapping[str, str]) -> str:
        source = (
            metadata.get("aria-label") or metadata.get("placeholder") or metadata.get("name")
            or metadata.get("id") or target.get("name") or target.get("value") or "value"
        )
        base = _NON_WORD.sub("_", str(source)).strip("_").lower()[:40] or "value"
        candidate = base
        suffix = 2
        while candidate in self._parameter_names:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _add_parameter(self, name: str, description: str) -> None:
        self._parameter_names.add(name)
        self._parameters.append(WorkflowParameter(
            name=name,
            type="string",
            required=True,
            description=description,
        ))

    @staticmethod
    def _target_description(target: Mapping[str, Any]) -> str:
        return str(target.get("aria_label") or target.get("placeholder") or target.get("name") or target.get("value") or "field")
