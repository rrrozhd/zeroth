# Usage Guide: Audit

## Overview

This guide shows how to query the [audit](../concepts/audit.md) trail for a given `run_id` — both in-process via `AuditRepository` and over HTTP via `GET /runs/{run_id}/timeline`. The records returned are the same `NodeAuditRecord` objects the [orchestrator](../concepts/orchestrator.md) wrote as it executed each node, so the timeline is the canonical replay log for a run.

## Minimal example

```python
from zeroth.core.audit import AuditQuery, AuditRepository

# 1. In-process: query by run_id using the repository directly.
audit_repo = AuditRepository(database)
records = await audit_repo.list(AuditQuery(run_id=run_id))

for record in records:
    enforcement = (record.execution_metadata or {}).get("enforcement")
    policy_note = ""
    if isinstance(enforcement, dict):
        policy_note = f" — policy: {enforcement.get('decision')}"
    print(f"[{record.node_id}] status={record.status}{policy_note}")
    if record.cost_usd is not None:
        print(f"  cost: ${record.cost_usd:.4f}  tokens: {record.token_usage}")
    for tc in record.tool_calls:
        print(f"  tool: {tc.alias}({tc.arguments}) -> {tc.outcome or tc.error}")

# 2. Over HTTP: the same records via the service API.
timeline = await client.get(
    f"/runs/{run_id}/timeline",
    headers={"X-API-Key": "demo-operator-key"},
)
timeline.raise_for_status()
for entry in timeline.json()["entries"]:
    print(entry["node_id"], entry["status"])
```

`list_by_run`, `list_by_thread`, `list_by_node`, `list_by_graph_version`, and `list_by_deployment` are one-line shortcuts wrapping `AuditQuery`. For a single node's detail, `audit_repo.get(audit_id)` returns one record or `None`.

## Common patterns

- **Build a timeline view.** Pass records to `AuditTimelineAssembler().assemble(run_id, records)` to get a `AuditTimeline` ordered by start time.
- **Produce compliance evidence.** `build_summary(records)` and `collect_policy_events(records)` flatten the trail into review-friendly blobs.
- **Verify chain integrity.** `AuditContinuityVerifier(audit_repo).verify_run(run_id)` walks `previous_record_digest` → `record_digest` and returns `AuditContinuityReport` — use it before exporting evidence.
- **Redact on write.** Configure `PayloadSanitizer(AuditRedactionConfig(redact_keys={"secret","token"}))` so secrets are scrubbed *before* persistence, not filtered out on read.

## Pitfalls

1. **Forgetting `deployment_ref`.** Cross-tenant queries must scope by `deployment_ref` or `tenant_id`; `run_id` alone is globally unique but leaks run counts across tenants.
2. **Mutating records.** `NodeAuditRecord` is immutable from the consumer's perspective; to "amend" an audit you must write a new record whose `supersedes_audit_id` points at the old one.
3. **Skipping the sanitizer.** If you bypass `ApprovalService` / orchestrator and write audit records directly, secret material can end up in `input_snapshot` — always run payloads through `PayloadSanitizer` first.
4. **Duplicate `audit_id`.** `AuditRepository.write` raises `ValueError` on duplicates; generate IDs via the default factory, never reuse.
5. **Assuming chain verification is automatic.** Chains are *computed* on write but only *verified* when you call `AuditContinuityVerifier`. Run the verifier before shipping evidence to a regulator.

## Reference cross-link

See the [Python API reference for `zeroth.core.audit`](../reference/python-api/audit.md).

- HTTP API: `GET /runs/{run_id}/timeline`, `GET /deployments/{ref}/audits?run_id=...`
- Related: [Concept: audit](../concepts/audit.md), [Usage Guide: policy](policy.md), [Usage Guide: approvals](approvals.md), [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md).
