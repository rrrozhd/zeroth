# Query the audit trail for a run

## What this recipe does
Writes three `NodeAuditRecord` rows against a SQLite-backed
`AuditRepository`, then walks the full trail for a run with
`list_by_run` and narrows to a single node with `AuditQuery`.

## When to use
- You're building a UI or CLI that needs to show every step of a
  run, in order, with inputs, outputs, and status.
- You're writing a compliance report that must enumerate every
  audit record for a tenant across a time range.
- You're debugging a production run and want to replay the decisions
  the orchestrator made.

## When NOT to use
- You only need the HTTP view — hit `GET /runs/{run_id}/timeline`
  directly instead of talking to the repository.
- You're streaming events in real time — subscribe to the audit
  emitter instead of polling the repository.

## Recipe
```python
--8<-- "24_audit_query.py"
```

## How it works
`AuditRepository` is a thin async wrapper around the audit table
created by Zeroth's Alembic migrations. `write` persists a
`NodeAuditRecord`; `list_by_run` loads every record for a run; and
`list(AuditQuery(...))` applies the optional filters (`run_id`,
`thread_id`, `node_id`, `graph_version_ref`, `deployment_ref`) in
SQL. All queries return fully-validated `NodeAuditRecord` objects.

## See also
- [Usage Guide: audit](../audit.md)
- [Concept: audit](../../concepts/audit.md)
- [Concept: runs](../../concepts/runs.md)
