# Phase 8 — Runtime Security Hardening Plan

## Goal

Move Zeroth from an MVP runtime with good security primitives into a production runtime that enforces isolation, policy-derived constraints, and secret protection under real adversarial assumptions.

## Why This Phase Exists

The MVP already has capability models, policy evaluation, audit redaction, and a sandbox abstraction. What it still lacks is hard enforcement across the runtime boundary: the sandbox still defaults to host-local subprocess execution, some policy outputs are logged rather than enforced, and secret handling remains mostly environment-based. A governed platform cannot stop at declarative policy.

## Scope

- Hardened sandbox backend defaults
- Enforced network, filesystem, timeout, and side-effect controls
- Secret management and at-rest data protection
- Executable-unit integrity and admission controls
- Security-oriented negative testing and attack-path validation

## Out Of Scope

- A full cloud-specific security product matrix
- Enterprise key-management integrations beyond the initial abstraction and at least one concrete provider

## Relevant Code Areas

- `src/zeroth/execution_units/sandbox.py`
- `src/zeroth/execution_units/runner.py`
- `src/zeroth/execution_units/models.py`
- `src/zeroth/policy/models.py`
- `src/zeroth/policy/guard.py`
- `src/zeroth/orchestrator/runtime.py`
- `src/zeroth/agent_runtime/tools.py`
- `src/zeroth/agent_runtime/runner.py`
- `src/zeroth/storage/sqlite.py`
- `src/zeroth/storage/redis.py`
- `tests/execution_units/`
- `tests/policy/`
- `tests/service/`

## Workstreams

### 8A. Hardened Sandbox Backend

Strengthen the executable-unit isolation boundary.

Requirements:

- Make a containerized or otherwise hardened backend the default for untrusted executable units
- Remove silent fallback to host-local execution for workloads that require isolation
- Enforce resource ceilings for CPU, memory, disk, and process spawn behavior
- Restrict workspace mounts, filesystem write scope, and network egress according to policy

### 8B. Policy-Derived Runtime Enforcement

Wire policy results into the runtime path instead of only recording them.

Requirements:

- Enforce `network_mode`, `timeout_override_seconds`, and secret allowlists in agent and executable-unit execution
- Gate side-effecting tool calls behind explicit approval requirements where policy demands it
- Ensure policy constraints survive retries, resume paths, and background execution
- Record both successful and denied enforcement actions in audit

### 8C. Secret Management And Data Protection

Reduce the amount of sensitive material that can leak through environment variables, storage, or audit.

Requirements:

- Replace raw secret values in manifests and runtime config with secret references
- Introduce a secret provider abstraction with at least one concrete implementation
- Protect secrets and sensitive run data at rest where practical for SQLite and other local stores
- Validate that checkpoints, approvals, and audits do not persist secret material accidentally

### 8D. Executable-Unit Integrity And Admission Control

Make executable units governable before they run, not only while they run.

Requirements:

- Add digests or signed metadata for executable-unit manifests and key artifacts
- Validate allowed runtimes, images, or commands before execution
- Reject untrusted or modified executable-unit definitions and record the reason

## Acceptance Criteria

- Untrusted executable units no longer rely on permissive host-local execution by default
- Policy-derived network, timeout, secret, and side-effect constraints are enforced in runtime behavior
- Secret material is reference-based and protected across service, runtime, and persistence layers
- Executable-unit integrity is checked before execution
- Security-focused tests demonstrate denial and containment behavior, not only success paths
