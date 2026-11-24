"""Browser control primitives for the UNBOUND AI backend."""

from .controller import BrowserController, BrowserControllerError, LocatorTarget
from .egress import DestinationPolicy, EgressDenied, LocalEgressProxy, ValidatedDestination, classify_address
from .downloads import BrowserDownloadResult, BrowserDownloadStore
from .recorder import BrowserRecorder
from .replay import (
    BrowserSessionBinding,
    BrowserSessionManager,
    CancellationChecker,
    ReplayPolicyHandoff,
    ReplayStepResult,
    WorkflowReplay,
    WorkflowReplayResult,
)
from .replay_state import (
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
from .replay_state_store import (
    ReplayActionLease,
    ReplayAuthorizationError,
    ReplayStateBusyError,
    ReplayStateConflictError,
    ReplayStateIntegrityError,
    ReplayStateNotFoundError,
    ReplayStateStore,
    ReplayStateStoreError,
    ReplayWorkflowMismatchError,
)
from .session_manager import BrowserSessionManagerError
from .tools import (
    BrowserClickInput,
    BrowserDownloadInput,
    BrowserFillInput,
    BrowserNavigateInput,
    BrowserRecordInput,
    BrowserReplayInput,
    BrowserToolAdapter,
    BrowserToolBinding,
    ReplayResumeContextProvider,
    TrustedReplayResumeContext,
    browser_tool_bindings,
    browser_tool_definitions,
)
from .verification import BrowserExpectation, BrowserVerificationResult, BrowserVerifier, VerificationCheck
from .workflow_store import (
    CorruptWorkflowError,
    DuplicateWorkflowError,
    SensitiveWorkflowDataError,
    WorkflowNotFoundError,
    WorkflowStore,
    WorkflowStoreError,
)

__all__ = [
    "BrowserController", "BrowserControllerError", "BrowserRecorder", "LocatorTarget",
    "CorruptWorkflowError", "DuplicateWorkflowError", "SensitiveWorkflowDataError",
    "WorkflowNotFoundError", "WorkflowStore", "WorkflowStoreError",
    "ReplayStepResult", "WorkflowReplay", "WorkflowReplayResult",
    "BrowserSessionBinding", "BrowserSessionManager", "CancellationChecker", "ReplayPolicyHandoff",
    "BrowserExpectation", "BrowserVerificationResult", "BrowserVerifier", "VerificationCheck",
    "BrowserDownloadResult", "BrowserDownloadStore",
    "DestinationPolicy", "EgressDenied", "LocalEgressProxy", "ValidatedDestination", "classify_address",
    "PreparedReplayAction", "ReplayActionJournalState", "ReplayActionOutcome",
    "ReplayContinuation", "ReplayExecutionResult", "ReplayPendingAction",
    "ReplayPolicyContext", "ReplayStepAuthorization", "ReplayStepCheckpoint",
    "ReplayActionLease", "ReplayAuthorizationError", "ReplayStateBusyError",
    "ReplayStateConflictError", "ReplayStateIntegrityError", "ReplayStateNotFoundError",
    "ReplayStateStore", "ReplayStateStoreError", "ReplayWorkflowMismatchError",
    "BrowserSessionManagerError",
    "BrowserClickInput", "BrowserDownloadInput", "BrowserFillInput", "BrowserNavigateInput",
    "BrowserRecordInput", "BrowserReplayInput", "BrowserToolAdapter", "BrowserToolBinding",
    "ReplayResumeContextProvider", "TrustedReplayResumeContext", "browser_tool_bindings",
    "browser_tool_definitions",
]
