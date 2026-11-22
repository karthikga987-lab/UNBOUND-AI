# Flagship Demo Scenario

## Flow

1. The user teaches UNBOUND AI a reusable website workflow.
2. The agent records the interactions and saves a reusable workflow definition.
3. The user uploads a PDF and a spreadsheet containing related information.
4. The user asks the agent to retrieve the latest report from the website.
5. The agent executes the saved workflow, validating parameters and verifying browser outcomes after actions.
6. The agent retrieves relevant information from the uploaded documents.
7. The agent compares website and document information, preserving sources and noting conflicts or missing data.
8. The agent generates a report with evidence references and citations.
9. The UI displays the execution timeline, verification state, evidence provenance, and final report artifact.

## Expected behavior

- The plan names only tools registered at runtime, with validated arguments and expected outcomes.
- Read-only navigation and retrieval may proceed without approval. Any purchase, payment, outbound communication, or consequential submission pauses for explicit approval even if auto-execution was requested.
- A tool call is not considered complete merely because it returned. Browser steps and other claims are verified; failed verification remains failed or needs attention.
- The final response separates verified facts from inferences, cites source evidence, and identifies assumptions or missing information. It does not invent a report, value, source, or missing parameter.
- Cancellation stops subsequent actions and is reflected in task state. Recoverable failures retain completed steps and evidence for resume.

## Failure and recovery cases

| Failure | Expected behavior |
|---|---|
| Website unavailable or login/session expired | Record the failed step and safe error; do not claim the report was retrieved. Request user action or allow a recoverable retry. |
| Locator missing or page layout changed | Use only approved locator fallback behavior; verify the resulting page/action. If verification fails, stop/replan or request attention. |
| Required workflow parameter missing | Ask for clarification before executing dependent steps. Do not invent a value. |
| Document is unsupported, corrupt, or unreadable | Report ingestion failure and continue only if the task can safely proceed without it; disclose incomplete comparison. |
| Website and document values conflict | Preserve both sources, label the discrepancy, and avoid asserting an unsupported resolution. |
| Sensitive action appears in the workflow | Pause at that step for explicit approval; auto-execute never bypasses the gate. |
| Browser/tool returns success but expected outcome is absent | Mark verification failed; do not mark the step/task completed. Preserve relevant tool evidence and error. |
| User cancels during execution | Stop before the next side effect, retain completed work and evidence, and set the task to cancelled. |
| Report generation fails | Retain gathered evidence and completed retrieval steps; report the missing artifact and allow recovery. |
