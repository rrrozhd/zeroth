# Phase 7 — Transparent Governance & Verifiable Provenance Plan

## Goal

Turn Zeroth's internal audit and policy data into externally inspectable, independently verifiable governance evidence.

## Why This Phase Exists

The MVP already records node-level audits, policy denials, approvals, and deployment metadata, but most of that transparency stays inside repositories and internal models. Public clients can inspect contracts and run status, yet they cannot retrieve complete audit timelines, verify deployment provenance, or detect audit tampering. That leaves Zeroth transparent by implementation intent, not by externally usable surface.

## Scope

- Public audit query and timeline APIs
- Governance evidence bundle export for runs and deployments
- Deployment snapshot digests and attestations
- Tamper-evident or append-only audit persistence semantics
- Verification flows and docs for operators and third-party reviewers

## Out Of Scope

- Full compliance certification packages
- GUI review tooling
- Long-term archival backends beyond the minimum needed for verifiable exports

## Relevant Code Areas

- `src/zeroth/audit/models.py`
- `src/zeroth/audit/repository.py`
- `src/zeroth/audit/timeline.py`
- `src/zeroth/service/app.py`
- `src/zeroth/service/bootstrap.py`
- `src/zeroth/service/run_api.py`
- `src/zeroth/service/contracts_api.py`
- `src/zeroth/deployments/models.py`
- `src/zeroth/deployments/service.py`
- `src/zeroth/orchestrator/runtime.py`
- `tests/audit/`
- `tests/service/`
- `docs/specs/`

## Workstreams

### 7A. Public Audit And Timeline API

Expose the audit system through stable deployment-scoped service routes.

Requirements:

- Add public endpoints for audit lookup by run, thread, node, deployment, and graph version
- Expose assembled audit timelines, not only raw records
- Apply the Phase 6 identity and authorization model to all audit access
- Keep audit responses redaction-aware and contract-validated

### 7B. Governance Evidence Surfaces

Make audit and policy information understandable, not only retrievable.

Requirements:

- Include policy decision details, tool-resolution lineage, approval decisions, and memory access summaries in review-friendly payloads
- Expose stable references from run status to full evidence bundles
- Add exportable evidence packages for a single run and a deployment snapshot

### 7C. Tamper-Evident Audit Storage

Strengthen the audit repository so governance evidence is not silently mutable.

Requirements:

- Replace upsert-style audit mutation semantics with append-only or otherwise tamper-evident storage
- Add record digests or hash chaining so mutation can be detected
- Define how corrected or superseding records are represented without rewriting history
- Add verification helpers that prove audit continuity for a run or deployment

### 7D. Deployment Provenance And Attestations

Make deployment snapshots independently verifiable.

Requirements:

- Add digest material for serialized graph snapshots and pinned contracts
- Produce deployment attestations that capture graph version, contract versions, policy set, and build identity
- Expose verification APIs or helpers so external systems can confirm a deployment still matches its attested snapshot

## Acceptance Criteria

- Operators can fetch audits and timelines through the public service surface
- Run and deployment evidence can be exported in a portable, reviewable form
- Audit persistence is append-only or tamper-evident rather than silently mutable
- Deployment snapshots carry verifiable provenance material
- Audit and provenance verification is covered by focused tests and documented for users
