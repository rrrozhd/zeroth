# Retention & Right-to-Erasure (WS-E)

Zeroth's audit trail is an **append-only SHA-256 hash-chain**: each
`NodeAuditRecord` hashes its own content and pins its predecessor's digest, so any
edit or deletion breaks continuity. That is exactly what a compliance buyer wants
for tamper-evidence — and exactly what makes GDPR **data-minimization** and the
**right to erasure (Art. 17)** hard: you cannot simply delete a record's PII
without snapping the chain.

WS-E resolves the tension with **commitment-digest crypto-erasure**, plus
per-tenant retention TTLs and legal holds. This document is deliberately honest
about what is and is not achieved.

## The mechanism: commitment-digest crypto-erasure

Every audit record carries a `digest_version`:

- **`digest_version = 1` (legacy, pre-WS-E rows).** The digest is the historical
  whole-payload SHA-256. These rows are **grandfathered and un-erasable** — they
  have no per-field commitments to fall back on, and re-hashing history to make
  them erasable would itself be a tamper event. `crypto_erase` raises on them.
- **`digest_version = 2` (all records written since WS-E).** At write time each
  PII field is replaced, *for digest purposes*, by
  `sha256(canonical_json(plaintext))` — a **commitment** — stored in
  `pii_commitments`. The record digest is computed over the commitments, **not**
  the raw plaintext.

Because the digest is over the commitments, nulling the plaintext later does not
change the digest. `crypto_erase` therefore:

1. nulls the PII payload fields (`input_snapshot`, `output_snapshot`,
   `validation_results`, `execution_metadata`, `stdout`, `stderr`, `error`,
   `tool_calls`, `memory_interactions`),
2. keeps `pii_commitments` and `record_digest` **unchanged**,
3. stamps `erased` / `erased_at` / `erasure_reason`,
4. writes the row back **without** touching `created_at`, `audit_id`,
   `previous_record_digest`, or `record_digest`.

The chain — and any WS-D keyed signature over the digest — **still verifies over
the tombstoned record**. `verify_run` returns `verified = True` with an unchanged
`record_count`. The digest seam lives in
`audit/verifier.py::_compute_record_digest` (the `_DIGEST_EXCLUDED_FIELDS` and
`_PII_COMMITMENT_FIELDS` gate).

### The residual limitation (do not overstate)

This is **commitment-hash crypto-erasure, not perfect erasure.** The retained
per-field commitment hashes are low-entropy for some fields (a boolean flag, a
short status string, a small enum). A hash of a low-entropy value is reversible by
brute force, so **a retained commitment may still constitute personal data** under
a strict reading of GDPR. We keep the commitments because they are what makes the
tamper-evidence survive erasure; we do not claim they are information-theoretically
void. Operators who need stronger guarantees for a specific field should avoid
writing that field into the audit payload in the first place.

Note also that the whole-field commit+null is **coarse**: erasing `tool_calls`
clears the non-PII `tool_ref`/`alias` alongside the PII `arguments`/`outcome`, so
the post-erasure evidence view loses tool-invocation structure, not only its PII.

## Full-surface erasure

`RetentionErasureService.erase_run(run_id, reason)` erases every PII surface a run
touches, in this order (artifact keys are harvested from the output snapshots
**before** they are nulled):

| Surface | Action |
|---|---|
| `node_audits` | crypto-erased (chain preserved) |
| `run_checkpoints` | **deleted** — the richest plaintext snapshot; the previously-missing cascade |
| `runs` row | redacted in place (`final_output`/`artifacts`/`metadata`/`error` nulled, row kept for continuity) |
| artifacts | `cleanup_run(run_id)` prefix sweep + per-key `delete` of references found in output snapshots |
| econ events | deleted via the optional econ hook (see below) |

Every step is **idempotent** and recorded in the append-only
`retention_audit_log`. A re-run reports zero newly-erased records rather than
double-counting.

## Granularity: run / tenant only

Right-to-erasure operates at **run** or **whole-tenant** granularity. There is
**no subject → record index** — a run is the finest unit that maps to a data
subject here. Erasing "all of a person's data" therefore requires the operator to
identify the relevant run(s) or tenant. A per-subject index is out of scope.

## Legal holds beat everything

A legal hold freezes data against deletion and **beats both TTL purge and explicit
erasure**:

- a run-scoped hold blocks erasure of that run;
- a tenant-wide hold (`run_id = NULL`) freezes every run for the tenant.

While a hold is active, `erase_run` raises `LegalHoldError` (surfaced as **HTTP
409** by `POST /v1/retention/erasure-requests`) and `purge_tenant` skips the held
runs. Releasing the hold re-enables erasure.

## Per-tenant retention TTLs

`retention_policies` holds one row per tenant plus a system-default row
(`tenant_id = 'default'`, seeded by migration 008 with keep-forever TTLs). A
tenant with no explicit policy inherits the default. The `RetentionPurgeWorker`
(started only when `ZEROTH_RETENTION__ENABLED=true`) sweeps every enabled policy
on `worker_poll_interval`, computing an `audit_ttl_seconds` cutoff against each
record's **`created_at`** (write time) and erasing aged, non-held runs. A `None`
TTL means keep forever.

## Econ-event coverage: in-scope vs deferred

The economic control plane (`zeroth.econ_plane`) records `execution_events` and
`outcome_events` on its **own** SQLAlchemy database, carrying tenant / cost /
potentially-PII payloads.

- **In scope / implemented.** The `EconEventEraser` interface
  (`delete_events_for_run(join_keys)`) and a concrete
  `SqlAlchemyEconEventEraser` that deletes both event tables by `join_key`. The
  erasure service calls it with the best-effort join keys it can derive from a run
  (the `run_id` itself plus any `join_key` found in audit `execution_metadata`).
- **Deferred (named).** Econ events are keyed by `join_key`, a **business-request**
  identifier resolved from runtime context, and there is **no durable
  `run_id → join_key` index**. Complete, automatic run→join_key resolution is
  therefore deferred, and the concrete eraser is **not wired into the default boot
  path** (so a base `zeroth-core` install without the `regulus` extra never takes
  a hard dependency on the econ plane). Until an operator wires
  `SqlAlchemyEconEventEraser` (or a resolver-backed equivalent) into
  `RetentionErasureService`, the erasure service records an `econ_erase_skipped`
  entry in the retention audit log rather than silently dropping the concern.

## API surface

All routes require `RETENTION_ADMIN` (admin-tier — erasure is irreversible) and
are tenant-scoped via `require_resource_scope`:

- `PUT/GET /v1/retention/policy`
- `POST /v1/retention/legal-holds`, `DELETE /v1/retention/legal-holds/{hold_id}`
- `POST /v1/retention/erasure-requests` — body `{run_id}` or `{tenant_id}`; returns
  **409** when an active legal hold covers the target.
