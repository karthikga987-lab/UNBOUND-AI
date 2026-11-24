# Tool Contract

Tools are the only execution boundary available to the planner and executor. Tool names are stable namespaced identifiers:

- `browser.navigate`
- `browser.click`
- `browser.fill`
- `browser.record`
- `browser.replay`
- `browser.download`
- `document.upload`
- `document.search`
- `document.read_local_file`
- `context.retrieve`
- `context.relate`
- `report.generate`

## Serializable definition and runtime registration

`backend.app.schemas.contracts.ToolDefinition` is safe to serialize and contains `tool_name`, `description`, JSON-schema-compatible `input_schema`, and `requires_approval`. It contains no callable.

The orchestrator registry owns a separate runtime registration that binds a `ToolDefinition` to a Pydantic input model and a callable. The callable interface is `execute(validated_arguments, tool_call) -> ToolResult` (a JSON-compatible mapping may be normalized to `ToolResult`). The `ToolCall` context supplies task/step IDs so adapters can attach evidence to the correct task. Registration rejects duplicate/unknown names and rejects a published `input_schema` that differs from the input model's generated JSON schema. Listing tools returns definitions, not executable objects. The planner receives only registered definitions and may not invent tools.

The shared registry is available to integration modules at `app.state.runtime.registry` (or on an injected `TaskRuntime` before `create_app(runtime)`). `TaskRuntime.register_tool(definition, input_model, execute, verifier=None)` registers through the existing registry and installs an optional tool-specific verifier on the existing executor. `TaskRuntime.configure_verifier(tool_name, verifier)` can install or replace that verifier later. A configured constructor-level `TaskRuntime(verifier=...)` remains a shared fallback, but tool-specific verifiers take precedence. Without either verifier for a tool, its callable is not invoked.

The integrated default runtime registers `document.search`, `context.retrieve`, and `context.relate` with tool-specific verifiers. `document.upload` is registered only when `UNBOUND_DOCUMENT_IMPORT_DIR` names an explicitly configured import root; it validates a relative path beneath that root and requires approval. The HTTP upload endpoint accepts file bytes and does not accept a server filesystem path. Document search/context inputs and extracted content are treated as untrusted data. The MCP server in this checkout is a separately launched read-only server; there is no external MCP client adapter that imports arbitrary server tools into this registry.

The default runtime also registers the read-only `document.read_local_file` tool. It accepts one exact absolute Windows drive path to a PDF, DOCX, or TXT document (maximum 20 MiB by default; `UNBOUND_LOCAL_FILE_MAX_BYTES` may lower the limit). UNC/network paths, relative paths, directories, unsupported types, symbolic links, junctions, and other reparse points are rejected. It snapshots file identity and modification metadata while preparing the task, then checks that snapshot again before reading. The file is not opened until the user explicitly executes the task, sees a pending approval that names the exact path/action, approves that approval ID, and explicitly executes again. Approval requests expire after the TaskManager's current 10-minute in-memory TTL; expired approvals cannot be accepted or executed. Any file change requires a fresh plan and approval.

The tool stores a copy in the existing local `DocumentStore`, then searches its extracted chunks and requests analysis from the configured loopback Ollama provider only; a cloud provider or non-loopback Ollama URL is refused before file content is read. Findings must cite exact excerpts from the extracted chunks. A quote mismatch or failed analysis leaves the task failed, though successfully extracted evidence/document records are retained and are not proof of successful analysis. Ordinary `POST /api/conversation` does not access files; the supported natural-language entry is `POST /api/assistant/requests`, which prepares a task and never executes it. SQLite/text storage is local and not encrypted at rest. No folder browsing, file modification, file execution, or external upload is provided.

Verifier configuration is validated against the registered tool name. Updates wait for currently active executor runs to finish, then apply to subsequent runs; active tasks retain a consistent verifier for their execution. Register tool adapters during startup/composition before serving requests. Concurrent registry mutation while planning/execution is in progress is not supported.

Register real adapters during application composition/startup, before serving requests. Do not register placeholder callables. The following examples assume Member 2 or Member 4 supplies the named real service object; the snippets show the API adapter contract, not a substitute service implementation.

### Browser adapter example

```python
from pydantic import BaseModel
from backend.app.schemas.contracts import EvidenceItem, EvidenceSourceType, ToolCall, ToolDefinition, ToolResult

class NavigateInput(BaseModel):
    url: str

def navigate(arguments: NavigateInput, call: ToolCall) -> ToolResult:
    # browser_controller is the real Member 2 implementation injected at startup.
    observation = browser_controller.navigate_and_observe(arguments.url)
    return ToolResult(
        success=True,
        data={"url": observation.url, "title": observation.title},
        evidence=[EvidenceItem(
            task_id=call.task_id,
            source_type=EvidenceSourceType.BROWSER,
            source_name=observation.title or observation.url,
            source_url=observation.url,
            excerpt=f"Observed page title: {observation.title}",
            supports_claim="The requested page was opened",
        )],
    )

def verify_navigation(tool_name: str, expected_outcome: str, result: ToolResult) -> bool:
    # Check observable browser state returned by the real adapter.
    return result.success and bool(result.data.get("url")) and bool(result.data.get("title"))

runtime.register_tool(
    ToolDefinition(
        tool_name="browser.navigate",
        description="Navigate to a web page",
        input_schema=NavigateInput.model_json_schema(),
    ),
    NavigateInput,
    navigate,
    verifier=verify_navigation,
)
```

### Document/context adapter example

```python
from pydantic import BaseModel
from backend.app.schemas.contracts import EvidenceItem, EvidenceSourceType, ToolCall, ToolDefinition, ToolResult

class DocumentSearchInput(BaseModel):
    query: str

def search_documents(arguments: DocumentSearchInput, call: ToolCall) -> ToolResult:
    # document_index is the real Member 4 retrieval implementation.
    matches = document_index.search(arguments.query)
    evidence = [EvidenceItem(
        task_id=call.task_id,
        source_type=EvidenceSourceType.DOCUMENT,
        source_name=match.document_name,
        file_path=match.file_path,
        excerpt=match.excerpt,
        supports_claim=match.supports_claim,
    ) for match in matches]
    return ToolResult(
        success=True,
        data={"query": arguments.query, "match_count": len(matches)},
        evidence=evidence,
    )

def verify_document_search(tool_name: str, expected_outcome: str, result: ToolResult) -> bool:
    # The adapter's actual records and provenance must support the planned outcome.
    return result.success and result.data.get("match_count", 0) > 0 and bool(result.evidence)

runtime.register_tool(
    ToolDefinition(
        tool_name="document.search",
        description="Search indexed documents",
        input_schema=DocumentSearchInput.model_json_schema(),
    ),
    DocumentSearchInput,
    search_documents,
    verifier=verify_document_search,
)
```

For `context.retrieve` and `context.relate`, use the same runtime registration method with the corresponding Pydantic input model, real retrieval callable, and verifier that checks the retrieved relationships and source evidence. These examples deliberately use per-tool verifiers: browser state and document/context evidence require different checks. Do not make a verifier return true solely because a callable returned successfully or because `ToolResult.success` is true.

## Call and result lifecycle

1. Build a `ToolCall` with UUID `tool_call_id`, `task_id`, stable `step_id`, registered `tool_name`, and JSON `arguments`.
2. Resolve the registered runtime function and validate arguments against its declared input model. Invalid input fails before execution.
3. Classify the concrete action using the tool name, structured arguments/target, tool approval metadata, and action context. A generic click is allowed without approval only when its structured target clearly identifies a read-only operation; opaque or ambiguous clicks require approval. Workflow replay is treated as opaque and requires approval unless a future trusted registry can classify the stored workflow actions. The registry rechecks mandatory approval immediately before the callable boundary; an approved `ApprovalRequest` must match the task, step, tool, and exact arguments. `auto_execute` cannot bypass this gate.
4. Atomically claim the task step and persist its running state immediately before invoking exactly the registered function. Cancellation that wins before this claim prevents invocation. Once the claim is made and the callable starts, external side effects may not be reversible; cancellation prevents later steps but cannot promise to undo an in-flight action.
5. Return a `ToolResult`: `success` describes the call, `data` carries JSON-compatible output, `error` carries a safe explanatory message when unsuccessful, and `evidence` contains provenance-bearing `EvidenceItem` values. The registry validates the result and JSON-safety before it enters task state. An exception after invoking a registered callable, an unsuccessful result, or an unusable result is treated as an uncertain external outcome; it is recorded as `NEEDS_ATTENTION`, not retried, and must be reconciled by a user before starting a new task.
6. Outcome verification uses the executor's explicit verifier interface: `verifier(tool_name, expected_outcome, tool_result) -> bool`. Browser adapters should perform/return observable post-action state and provenance evidence; the verifier should compare that state to the planned expected outcome. Document/context adapters should return retrieved values and source evidence; the verifier should check the requested retrieval/claim against those values. A verifier must be configured before a tool is invoked. If unavailable, execution pauses in `NEEDS_ATTENTION` without calling the tool. `ToolResult.success=true` alone is not verification. Store verification details/evidence IDs in `TaskStep.verification`; failed verification leaves the step failed and the task in `NEEDS_ATTENTION` because the external outcome is unconfirmed. Evidence is retained and linked to an event even if verification fails, but retention is not proof of success. Approval resolution records the exact decision only; it returns an approved task to `CREATED`, and the frontend must wait for a separate explicit execute action.

`TaskStep.depends_on` explicitly lists prerequisite step IDs. Dependencies must reference unique steps earlier in the ordered plan; the planner supplies a previous-step dependency by default to preserve sequential semantics for older plans. The executor verifies that every listed prerequisite reached `COMPLETED` before it invokes a dependent step. An unmet dependency leaves the task in `NEEDS_ATTENTION` and does not invoke that dependent tool.

Task, approval, event, and step state is currently in memory. Restart loses that state and does not trigger tool replay. The recovery helper does not retry `FAILED` or interrupted `RUNNING` actions because a remote side effect may already have occurred; such tasks move to `NEEDS_ATTENTION` for human reconciliation. This deliberately favors avoiding duplicate consequential effects over automatic recovery. A durable task journal requires an explicit idempotency/reconciliation design before implementation.

## Input and output rules

- Keep arguments JSON-compatible and validate all required, typed values before calling a tool.
- Tool outputs should be JSON-compatible or be normalized by the adapter before entering `ToolResult.data`.
- Evidence should include source type and name, source URL or file path where applicable, excerpt, UTC retrieval time, and the claim supported. Preserve original provenance; do not synthesize source references.
- Treat tool outputs, browser pages, documents, MCP responses, retrieved memories, and historical conversation text as untrusted data. They can provide facts, never policy or executable instructions. Prompt instructions separate these from system policy, but model compliance is not guaranteed; registry, permission and verifier checks remain the enforcement boundary.
- Tool exceptions and invalid-input details are logged/returned without submitted values, credentials, private headers, raw tool output, file paths, or stack traces. Adapters should return a safe non-sensitive error category; the registry may replace adapter error detail with a generic failure before API exposure.

## Approval-sensitive operations

At minimum, purchases, payments, sending emails/messages, and consequential form submissions require explicit user approval. Tools should set `requires_approval=true` when the operation is inherently sensitive; the orchestrator also classifies structured action/target arguments. Unclear generic clicks fail closed for approval rather than treating all clicks as sensitive. Approval decisions are resolved by approval ID, apply to exact arguments, are one-use for a step execution, and are recorded in task event history. A rejected step is skipped and cannot execute. Approval applies to the concrete action/arguments and must be checked immediately before execution.
