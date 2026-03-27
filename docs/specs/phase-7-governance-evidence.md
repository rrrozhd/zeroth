# Phase 7 Governance Evidence Guide

This document describes the public Phase 7 governance surfaces exposed by the deployment-bound service API.

## Audit And Timeline Endpoints

- `GET /deployments/{deployment_ref}/audits`
  Returns deployment-scoped audit records. Supports `run_id`, `thread_id`, `node_id`, and `graph_version_ref` filters.
- `GET /runs/{run_id}/timeline`
  Returns the ordered audit timeline for one run.
- `GET /deployments/{deployment_ref}/timeline`
  Returns the ordered audit timeline for the active deployment snapshot.

Audit responses are read-protected by the Phase 6 identity model and redact sensitive keys such as `secret`, `token`, `password`, `api_key`, and `authorization`.

## Evidence Bundle Endpoints

- `GET /runs/{run_id}/evidence`
  Returns the public run payload, redacted audit records, related approvals, governance summary counts, and extracted policy or authorization events.
- `GET /deployments/{deployment_ref}/evidence`
  Returns the public deployment metadata payload, redacted deployment-scoped audits, related approvals, run lineage, and the same summary view.

Discoverability links are exposed directly on the existing public surfaces:

- Run status payloads now include `timeline_ref` and `evidence_ref`
- Deployment metadata payloads now include `audit_ref`, `timeline_ref`, `evidence_ref`, and `attestation_ref`

## Attestation Endpoints

- `GET /deployments/{deployment_ref}/attestation`
  Returns the stable attestation payload for the bound deployment snapshot.
- `POST /deployments/{deployment_ref}/verify-attestation`
  Accepts an attestation payload and returns:
  - `verified`: boolean
  - `mismatches`: list of attestation fields that no longer match the persisted snapshot

Deployment attestations include:

- deployment identity and version
- graph identity and pinned graph version
- pinned input and output contract refs and versions
- `graph_snapshot_digest`
- `contract_snapshot_digest`
- `settings_snapshot_digest`
- `attestation_digest`

## Verification Notes

- Audit persistence is append-only by `audit_id`; duplicate writes are rejected.
- Each audit record carries `previous_record_digest`, `record_digest`, and optional `supersedes_audit_id`.
- Continuity verification is available internally through `AuditContinuityVerifier` for run and deployment history checks.
- Attestation verification recomputes digests from the current persisted deployment snapshot rather than trusting stored digest columns, so direct database tampering is detected.
