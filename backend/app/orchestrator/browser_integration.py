"""Production composition seam for the browser engine and orchestrator.

This module owns dependency construction and registration. It adds no routes and
does not replace TaskManager, ToolRegistry, or the approval lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID

from backend.app.browser.replay import WorkflowReplay
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.tools import BrowserToolAdapter, browser_tool_bindings
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.browser_replay import BrowserReplayOrchestratorBridge
from backend.app.orchestrator.executor import TaskExecutor, Verifier
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import TaskStatus


@dataclass
class BrowserOrchestratorIntegration:
    """Owned browser dependencies and the executor configured to use them."""

    workflow_store: WorkflowStore
    session_manager: BrowserSessionManager
    replay_state_store: ReplayStateStore
    replay_bridge: BrowserReplayOrchestratorBridge
    replay_engine: WorkflowReplay
    adapter: BrowserToolAdapter
    executor: TaskExecutor

    def shutdown(self) -> None:
        """Application shutdown hook; safe to invoke more than once."""
        self.adapter.cleanup_all()
        self.session_manager.close_all()


def register_browser_integration(
    registry: ToolRegistry,
    task_manager: TaskManager,
    verifier: Verifier | None,
    *,
    controller_factory: Callable | None = None,
    workflow_directory: str | Path | None = None,
    replay_state_directory: str | Path | None = None,
    workflow_store: WorkflowStore | None = None,
    session_manager: BrowserSessionManager | None = None,
    replay_state_store: ReplayStateStore | None = None,
) -> BrowserOrchestratorIntegration:
    """Construct browser dependencies and register exactly the approved tools.

    State encryption configuration is required by ReplayStateStore and fails
    closed when its key is absent or invalid. Stores and session manager may be
    injected by the host for testing or controlled deployments.
    """
    if not isinstance(registry, ToolRegistry) or not isinstance(task_manager, TaskManager):
        raise TypeError("Existing ToolRegistry and TaskManager instances are required")

    expected_names = {
        "browser.navigate", "browser.click", "browser.fill", "browser.record",
        "browser.replay", "browser.download",
    }
    existing = {item.tool_name for item in registry.list_tools()}
    conflict = expected_names & existing
    if conflict:
        raise ValueError("Browser tools are already registered")

    workflows = workflow_store or WorkflowStore(workflow_directory)
    sessions = session_manager or BrowserSessionManager(controller_factory)
    bridge = BrowserReplayOrchestratorBridge(
        task_manager,
        workflows,
        sessions,
    )
    replay_store = replay_state_store or ReplayStateStore(
        replay_state_directory,
        authorization_validator=bridge.validate_authorization,
        session_validator=bridge.validate_session,
    )
    # Inject trusted validators even for a host-supplied isolated store.
    replay_store.authorization_validator = bridge.validate_authorization
    replay_store.session_validator = bridge.validate_session
    bridge.attach_state_store(replay_store)
    engine = WorkflowReplay(
        workflows,
        replay_state_store=replay_store,
        session_manager=sessions,
        cancellation_checker=bridge,
        policy_handoff=bridge,
    )
    adapter = BrowserToolAdapter(
        workflow_store=workflows,
        session_manager=sessions,
        replay_engine=engine,
        policy_handoff=bridge,
        resume_context_provider=bridge,
    )

    bindings = browser_tool_bindings(adapter)
    if {binding.definition.tool_name for binding in bindings} != expected_names or len(bindings) != 6:
        raise RuntimeError("Browser adapter tool namespace does not match the approved set")
    # Validate all bindings before mutating the registry so schema/configuration
    # errors do not leave a partial browser registration.
    for binding in bindings:
        if binding.definition.input_schema != binding.input_model.model_json_schema():
            raise ValueError("Browser adapter input schema is inconsistent")
    for binding in bindings:
        registry.register(binding.definition, binding.input_model, binding.execute)

    executor = TaskExecutor(
        task_manager,
        registry,
        verifier,
        browser_replay_bridge=bridge,
    )

    def cleanup(task_id: UUID, status: TaskStatus) -> None:
        del status
        adapter.cleanup_task(task_id)
        sessions.close_task_session(task_id)
        bridge.forget_task(task_id)

    task_manager.register_terminal_callback(cleanup)
    return BrowserOrchestratorIntegration(
        workflow_store=workflows,
        session_manager=sessions,
        replay_state_store=replay_store,
        replay_bridge=bridge,
        replay_engine=engine,
        adapter=adapter,
        executor=executor,
    )
