"""Persistent JSON storage for shared workflow definitions."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import ValidationError

from backend.app.schemas.contracts import WorkflowDefinition


_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|token|secret|authorization|credential|cookie|session|"
    r"api.?key|private.?key|access.?key|client.?key|refresh.?key|bearer|jwt|"
    r"cvv|cvc|card.?number|security.?code|social.?security|\bssn\b|"
    r"national.?id|tax.?id|bank.?account|routing.?number)",
    re.IGNORECASE,
)
_PARAMETER_PLACEHOLDER = re.compile(r"^\{\{[a-zA-Z0-9_]+\}\}$")
_STORE_LOCK = RLock()


class WorkflowStoreError(RuntimeError):
    """Base error for safe workflow storage failures."""


class WorkflowNotFoundError(WorkflowStoreError, LookupError):
    pass


class DuplicateWorkflowError(WorkflowStoreError):
    pass


class CorruptWorkflowError(WorkflowStoreError):
    pass


class SensitiveWorkflowDataError(WorkflowStoreError):
    pass


def _workflow_key(workflow_id: UUID | str) -> str:
    try:
        parsed = UUID(str(workflow_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("workflow_id must be a UUID4") from exc
    if parsed.version != 4:
        raise ValueError("workflow_id must be a UUID4")
    return str(parsed)


def _contains_sensitive_value(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"url", "href", "start_url"} and isinstance(child, str):
                try:
                    parts = urlsplit(child)
                    if parts.username is not None or parts.password is not None:
                        return True
                    if any(
                        _SENSITIVE_KEY.search(query_key) and query_value
                        for query_key, query_value in parse_qsl(parts.query, keep_blank_values=True)
                    ):
                        return True
                except ValueError:
                    # The shared action input is extensible; malformed URL-like
                    # values are validated by the adapter that consumes them.
                    pass
            if _SENSITIVE_KEY.search(str(key)) and child not in (None, ""):
                if isinstance(child, str) and _PARAMETER_PLACEHOLDER.fullmatch(child):
                    continue
                return True
            if _contains_sensitive_value(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_value(child) for child in value)
    return False


def _validate_no_obvious_secrets(workflow: WorkflowDefinition) -> None:
    """Reject common credential fields without echoing their values in errors."""
    url = urlsplit(str(workflow.start_url))
    if url.username is not None or url.password is not None:
        raise SensitiveWorkflowDataError("Workflow URL contains credentials and cannot be stored")
    if any(_SENSITIVE_KEY.search(key) and value for key, value in parse_qsl(url.query, keep_blank_values=True)):
        raise SensitiveWorkflowDataError("Workflow URL contains a sensitive query value and cannot be stored")

    for parameter in workflow.parameters:
        if _SENSITIVE_KEY.search(parameter.name) and parameter.default not in (None, ""):
            if not (isinstance(parameter.default, str) and _PARAMETER_PLACEHOLDER.fullmatch(parameter.default)):
                raise SensitiveWorkflowDataError("Workflow contains a sensitive parameter default and cannot be stored")

    for step in workflow.steps:
        target = step.target or {}
        is_sensitive_target = any(_SENSITIVE_KEY.search(str(value)) for value in target.values())
        if is_sensitive_target and _contains_plain_value(step.input):
            raise SensitiveWorkflowDataError("Workflow contains a value for a sensitive target and cannot be stored")

    payload = [step.model_dump(mode="json") for step in workflow.steps]
    if _contains_sensitive_value(payload):
        raise SensitiveWorkflowDataError("Workflow contains a value in a sensitive field and cannot be stored")


def _contains_plain_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_plain_value(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_plain_value(child) for child in value)
    if isinstance(value, str):
        return bool(value) and not _PARAMETER_PLACEHOLDER.fullmatch(value)
    return value is not None


class WorkflowStore:
    """Save workflows under ``backend/generated_data/workflows`` by default.

    Tests and isolated deployments can inject a directory. Workflow IDs are
    normalized UUID4 values and never used as unchecked path fragments.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (
            Path(directory)
            if directory is not None
            else Path(__file__).resolve().parents[2] / "generated_data" / "workflows"
        )

    def save(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        """Validate and atomically write one workflow; duplicate IDs are rejected."""
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError("workflow must be a WorkflowDefinition")
        try:
            normalized = WorkflowDefinition.model_validate(workflow.model_dump(mode="json"))
            encoded = normalized.model_dump_json(indent=2)
        except Exception as exc:
            raise WorkflowStoreError("Workflow could not be serialized for storage") from exc
        _validate_no_obvious_secrets(normalized)
        key = _workflow_key(normalized.workflow_id)
        path = self.directory / f"{key}.json"
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with _STORE_LOCK:
                if path.exists():
                    raise DuplicateWorkflowError(f"Workflow '{key}' already exists")
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{key}.", suffix=".tmp", dir=self.directory
                )
                temporary_path = Path(temporary_name)
                try:
                    with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary_path, 0o600)
                    # Same-directory replace publishes a complete JSON file at once.
                    os.replace(temporary_path, path)
                finally:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except DuplicateWorkflowError:
            raise
        except OSError as exc:
            raise WorkflowStoreError("Could not save workflow to generated storage") from exc
        return normalized.model_copy(deep=True)

    def list_workflows(self) -> list[WorkflowDefinition]:
        """Return validated workflows in deterministic workflow-ID order."""
        if not self.directory.exists():
            return []
        if not self.directory.is_dir():
            raise WorkflowStoreError("Workflow storage location is not a directory")
        workflows: list[WorkflowDefinition] = []
        seen: set[str] = set()
        try:
            paths = sorted(self.directory.glob("*.json"), key=lambda path: path.name)
        except OSError as exc:
            raise WorkflowStoreError("Could not list generated workflows") from exc
        for path in paths:
            if path.is_symlink():
                raise CorruptWorkflowError("Workflow storage contains an invalid linked file")
            workflow = self._read_file(path)
            key = _workflow_key(workflow.workflow_id)
            if key in seen:
                raise CorruptWorkflowError("Workflow storage contains duplicate workflow IDs")
            if path.name != f"{key}.json":
                raise CorruptWorkflowError("Workflow filename does not match its workflow ID")
            seen.add(key)
            workflows.append(workflow)
        return workflows

    def load(self, workflow_id: UUID | str) -> WorkflowDefinition:
        key = _workflow_key(workflow_id)
        path = self.directory / f"{key}.json"
        if not path.exists():
            raise WorkflowNotFoundError(f"Workflow '{key}' was not found")
        if path.is_symlink():
            raise CorruptWorkflowError("Workflow storage contains an invalid linked file")
        workflow = self._read_file(path)
        if _workflow_key(workflow.workflow_id) != key:
            raise CorruptWorkflowError("Stored workflow ID does not match its filename")
        return workflow

    def delete(self, workflow_id: UUID | str) -> bool:
        """Delete a workflow; return False when its file does not exist."""
        key = _workflow_key(workflow_id)
        path = self.directory / f"{key}.json"
        with _STORE_LOCK:
            try:
                if path.is_symlink():
                    raise CorruptWorkflowError("Workflow storage contains an invalid linked file")
                if not path.exists():
                    return False
                path.unlink(missing_ok=True)
                return True
            except CorruptWorkflowError:
                raise
            except OSError as exc:
                raise WorkflowStoreError("Could not delete workflow from generated storage") from exc

    @staticmethod
    def _read_file(path: Path) -> WorkflowDefinition:
        try:
            encoded = path.read_text(encoding="utf-8")
            workflow = WorkflowDefinition.model_validate_json(encoded)
            _validate_no_obvious_secrets(workflow)
            return workflow
        except (OSError, UnicodeDecodeError, ValidationError, ValueError, SensitiveWorkflowDataError) as exc:
            raise CorruptWorkflowError(f"Workflow file '{path.name}' is invalid or unreadable") from exc
