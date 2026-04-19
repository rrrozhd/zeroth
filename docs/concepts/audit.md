# Audit

## What it is

The **audit** subsystem records a `NodeAuditRecord` for every node execution in a [run](runs.md): inputs, outputs, tool calls, memory accesses, approval actions, policy enforcement, token usage, cost, timing, and errors. Records are append-only, hash-chained, and queryable by run, thread, node, graph version, or deployment.

Every write goes through `PayloadSanitizer` first, so secrets never reach the database, and every record is linked to the previous one via `previous_record_digest → record_digest` so tampering is detectable after the fact.

## Why it exists

"The agent did something weird last Tuesday" is not a debuggable statement.

Zeroth's answer is to make every run replayable: pull the `AuditTimeline` for a `run_id` and you see exactly which nodes fired, in what order, with what inputs, under which [policy](policy.md) decision, with which reviewer's [approval](approvals.md), and with which tool calls and costs attached. Because each record's `record_digest` is chained from the previous record, tampering is detectable via `AuditContinuityVerifier`.

This is the evidence layer that makes Zeroth runnable in regulated environments — and the debugging layer that makes it tolerable in any environment.

## Where it fits

Audit records attach to [runs](runs.md) via `run_id` and are written by the [orchestrator](orchestrator.md) after each node completes (and after policy denials, and after approval actions). The `PayloadSanitizer` strips secrets before persistence, honoring `AuditRedactionConfig`.

Reviewers fetch records through the `AuditRepository.list(AuditQuery(...))` API or the HTTP equivalents `GET /runs/{run_id}/timeline` and `GET /deployments/{ref}/audits`. `AuditTimelineAssembler` orders records into a coherent per-run story; `build_summary` and `collect_policy_events` produce evidence blobs for compliance reviews.

## Key types

- **`NodeAuditRecord`** — one immutable row per node execution; the canonical audit unit.
- **`AuditQuery`** — filter by `run_id`, `thread_id`, `node_id`, `graph_version_ref`, or `deployment_ref`.
- **`AuditTimeline` / `AuditTimelineAssembler`** — time-ordered replay log for a scope.
- **`AuditRepository`** — async storage; `write`, `list`, `list_by_run`, `list_by_deployment`.
- **`PayloadSanitizer` / `AuditRedactionConfig`** — redaction of secret keys and omit-paths before write.
- **`AuditContinuityVerifier` / `AuditContinuityReport`** — chain-digest verification for tamper detection.
- **`ToolCallRecord` / `MemoryAccessRecord` / `ApprovalActionRecord`** — nested sub-records that live inside `NodeAuditRecord`.
- **`build_summary` / `collect_policy_events`** — helpers in `zeroth.core.audit.evidence` that flatten a timeline into compliance-ready views.

## See also

- [Usage Guide: audit](../how-to/audit.md) — query the audit trail for a completed run.
- [Concept: runs](runs.md) — audit entries are attached to runs.
- [Concept: policy](policy.md) — enforcement decisions land in `execution_metadata["enforcement"]`.
- [Concept: approvals](approvals.md) — approval actions are stamped into audit records.
- [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md) — end-to-end policy + approval + audit story.
