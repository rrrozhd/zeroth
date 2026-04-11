# Python API Reference

Auto-generated from docstrings via [mkdocstrings](https://mkdocstrings.github.io/) + [Griffe](https://mkdocstrings.github.io/griffe/). Every public symbol in `zeroth.core.*` is documented here, grouped by subsystem.

## Subsystems

### Execution core
- [Graph](python-api/graph.md) — `zeroth.core.graph`
- [Orchestrator](python-api/orchestrator.md) — `zeroth.core.orchestrator`
- [Agents](python-api/agents.md) — `zeroth.core.agent_runtime`
- [Execution units](python-api/execution-units.md) — `zeroth.core.execution_units`
- [Conditions](python-api/conditions.md) — `zeroth.core.conditions`

### Data & state
- [Mappings](python-api/mappings.md) — `zeroth.core.mappings`
- [Memory](python-api/memory.md) — `zeroth.core.memory`
- [Storage](python-api/storage.md) — `zeroth.core.storage`
- [Contracts](python-api/contracts.md) — `zeroth.core.contracts`
- [Runs](python-api/runs.md) — `zeroth.core.runs`

### Governance
- [Policy](python-api/policy.md) — `zeroth.core.policy`
- [Approvals](python-api/approvals.md) — `zeroth.core.approvals`
- [Audit](python-api/audit.md) — `zeroth.core.audit`
- [Guardrails](python-api/guardrails.md) — `zeroth.core.guardrails`
- [Identity](python-api/identity.md) — `zeroth.core.identity`

### Platform
- [Secrets](python-api/secrets.md) — `zeroth.core.secrets`
- [Dispatch](python-api/dispatch.md) — `zeroth.core.dispatch`
- [Economics](python-api/econ.md) — `zeroth.core.econ`
- [Service](python-api/service.md) — `zeroth.core.service`
- [Webhooks](python-api/webhooks.md) — `zeroth.core.webhooks`

## How this is generated

Pages are rendered at build time from Python docstrings. See `mkdocs.yml` (`mkdocstrings` plugin) for configuration. Docstring coverage is gated at ≥90% via `interrogate` (see Phase 27).
