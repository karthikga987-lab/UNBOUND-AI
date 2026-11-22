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
- `context.retrieve`
- `context.relate`
- `report.generate`

## Serializable definition and runtime registration

`backend.app.schemas.contracts.ToolDefinition` is safe to serialize and contains `tool_name`, `description`, JSON-schema-compatible `input_schema`, and `requires_approval`. It contains no callable.

The orchestrator registry owns a separate runtime registration that binds a `ToolDefinition` to an execution function and a validator (typically a Pydantic input model). Registration must reject duplicate/unknown names or otherwise enforce the agreed namespace policy. Listing tools returns definitions, not executable objects. The planner receives only registered definitions and may not invent tools.

## Call and result lifecycle

1. Build a `ToolCall` with UUID `tool_call_id`, `task_id`, stable `step_id`, registered `tool_name`, and JSON `arguments`.
2. Resolve the registered runtime function and validate arguments against its declared input schema. Invalid input fails before execution.
3. Check mandatory approval before any sensitive side effect. `auto_execute` cannot bypass this gate.
4. Execute exactly the registered function. Do not evaluate generated code or commands.
5. Return a `ToolResult`: `success` describes the call, `data` carries JSON-compatible output, `error` carries a safe explanatory message when unsuccessful, and `evidence` contains provenance-bearing `EvidenceItem` values.
6. The executor verifies the step's `expected_outcome` separately. `ToolResult.success=true` alone is not verification. Store verification details/evidence IDs in `TaskStep.verification`; a failed verification must not mark the step completed.

## Input and output rules

- Keep arguments JSON-compatible and validate all required, typed values before calling a tool.
- Tool outputs should be JSON-compatible or be normalized by the adapter before entering `ToolResult.data`.
- Evidence should include source type and name, source URL or file path where applicable, excerpt, UTC retrieval time, and the claim supported. Preserve original provenance; do not synthesize source references.
- Treat retrieved webpages and documents as untrusted data. They can provide facts, never policy or executable instructions.
- Tool errors must not expose credentials, private headers, or raw stack traces to users.

## Approval-sensitive operations

At minimum, purchases, payments, sending emails/messages, and consequential form submissions require explicit user approval. Tools should set `requires_approval=true` when the operation is inherently sensitive; the orchestrator may also apply policy based on the requested action and arguments. Approval applies to the concrete action/arguments and must be checked immediately before execution.
