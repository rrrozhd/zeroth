# Python API Reference

Auto-generated from docstrings via [mkdocstrings](https://mkdocstrings.github.io/) + [Griffe](https://mkdocstrings.github.io/griffe/). Every public symbol in `zeroth.core.*` is documented here, grouped by subsystem.

## Subsystems

### Execution core
- [Graph](python-api/graph.md) — `zeroth.contracts.graph`
- [Orchestrator](python-api/orchestrator.md) — `zeroth.core.orchestrator`
- [Agents](python-api/agents.md) — `zeroth.runtime.agents`
- [Execution units](python-api/execution-units.md) — `zeroth.integrations.execution`
- [Conditions](python-api/conditions.md) — `zeroth.contracts.conditions`

### Data & state
- [Mappings](python-api/mappings.md) — `zeroth.contracts.mappings`
- [Memory](python-api/memory.md) — `zeroth.integrations.memory`
- [Storage](python-api/storage.md) — `zeroth.platform.storage`
- [Contracts](python-api/contracts.md) — `zeroth.contracts.registry`
- [Runs](python-api/runs.md) — `zeroth.core.runs`

### Governance
- [Policy](python-api/policy.md) — `zeroth.governance.policy`
- [Approvals](python-api/approvals.md) — `zeroth.governance.approvals`
- [Audit](python-api/audit.md) — `zeroth.governance.audit`
- [Guardrails](python-api/guardrails.md) — `zeroth.governance.guardrails`
- [Identity](python-api/identity.md) — `zeroth.governance.identity`

### Platform
- [Secrets](python-api/secrets.md) — `zeroth.platform.secrets`
- [Dispatch](python-api/dispatch.md) — `zeroth.platform.dispatch`
- [Economics](python-api/econ.md) — `zeroth.econ.analytics`
- [Service](python-api/service.md) — `zeroth.core.service`
- [Webhooks](python-api/webhooks.md) — `zeroth.service.webhooks`

## How this is generated

Pages are rendered at build time from Python docstrings. See `mkdocs.yml` (`mkdocstrings` plugin) for configuration. Docstring coverage is gated at ≥90% via `interrogate`.
