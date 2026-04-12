# Zeroth runnable examples

Every file in this directory uses only the **real public API** of the
Zeroth library — no stub runners, no `litellm.completion` shortcuts, no
fake `_EchoUnitRunner` placeholders. If an example prints
`COMPLETED`, the same call shape would work against a production
service.

The files are numbered so you can read them in progression: core
runtime → ship it as an API → governance → advanced.

## Reading order

### 0× · Core runtime — learn the shape

| File                              | What it teaches                                                              | LLM key? |
| --------------------------------- | ---------------------------------------------------------------------------- | :------: |
| `00_hello.py`                     | One `AgentNode`, one `AgentRunner`, one `LiteLLMProviderAdapter`. The minimum viable honest example. | ✓ |
| `01_first_graph.py`               | `AgentNode` → `ExecutableUnitNode`, with a real `EdgeMapping` and a real native tool. | ✓ |
| `02_multi_agent.py`               | Two-agent handoff: researcher → writer, driven by the orchestrator. No out-of-band calls. | ✓ |
| `03_conditional_branches.py`      | `Condition`-edged graph; orchestrator picks the lane at runtime. Hermetic (uses `DeterministicProviderAdapter`). |   |
| `04_native_tool.py`               | `NativeUnitManifest` + `ToolAttachmentBridge` + `tool_executor` wired onto a real `AgentRunner`. |   |
| `05_memory.py`                    | `ThreadMemoryConnector` + `MemoryConnectorResolver` attached to an agent; second run sees what the first wrote. |   |

### 1× · Service tier — ship it as an API

| File                                 | What it teaches                                                             |
| ------------------------------------ | --------------------------------------------------------------------------- |
| `10_serve_in_python.py`              | `bootstrap_service` → `create_app` → `uvicorn.run`. Prints the curl commands you need. |
| `11_serve_via_entrypoint.md`         | The production path: `zeroth.yaml`, `ZEROTH_*` env vars, `python -m examples.service.entrypoint`. |
| `12_docker_compose.md`               | How the `docker-compose.yml` stack fits together and where each service plugs in. |
| `13_dev_server.py`                   | The fastest "change a graph, see the result" inner loop, without HTTP.       |
| `service/zeroth.yaml`                | Base config file picked up by `zeroth.core.config.settings.get_settings`.   |
| `service/seed_deployment.py`         | One-shot: migrations → contracts → graph → publish → deploy.                |
| `service/entrypoint.py`              | Drop-in extension of `zeroth.core.service.entrypoint` with your own agent runners. |

### 2× · Governance tier — production-grade

| File                              | What it teaches                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------- |
| `20_approval_gate.py`             | `HumanApprovalNode` pauses a run; example resolves it via a real uvicorn + real `POST /v1/deployments/.../approvals/.../resolve`. |
| `21_policy_block.py`              | `PolicyGuard` denies a node at orchestrator dispatch time. Run ends `FAILED` with `policy_violation`. |
| `22_budget_cap.py`                | `BudgetEnforcer` wired onto `AgentRunner.budget_enforcer`. Over-budget run blocked before the provider is called. Uses `httpx.MockTransport` to mimic the Regulus backend. |
| `23_secrets_and_sandbox.py`       | `SecretResolver` → `SandboxManager.run(...)` → `SecretRedactor` showing how secrets flow through the real pipeline and get redacted from audit payloads. |
| `24_audit_query.py`               | Drive a real graph, then query `AuditRepository` both fully and by node id.     |
| `25_webhook_delivery.py`          | Real `WebhookDeliveryWorker` hitting a real receiver (HMAC verified), including the retry-then-succeed path. |
| `26_governance_walkthrough.py`    | Umbrella that runs 20 + 21 + 22 + 24 in sequence.                               |

### 3× · Advanced

| File                              | What it teaches                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------- |
| `30_contracts_and_mappings.py`    | All four `MappingOperation` types in one graph: passthrough, rename, constant, default. |
| `31_guardrails.py`                | `GuardrailConfig` + `DeadLetterManager.handle_run_failure` transitioning a run to `dead_letter`. |
| `32_observability.py`             | `MetricsCollector` + correlation IDs; renders Prometheus exposition text.       |
| `33_mcp_tools.py`                 | `MCPServerConfig` wired onto an `AgentConfig`. Points at the real discovery flow in `zeroth.core.agent_runtime.mcp`. |

## Shared helpers

* **`_common.py`** — collapses the 40-line SQLite-migrate-contracts-graph-deploy-bootstrap dance into one `running_service(...)` async context manager. Read it once and you'll understand what every numbered example is doing under the hood.
* **`_contracts.py`** — the Pydantic models every example imports from instead of inventing throwaway `DemoPayload` classes.
* **`_tools.py`** — two native Python-callable tools (`eu://format_article`, `eu://echo`) registered via `NativeUnitManifest`, ready to drop onto any `ExecutableUnitNode`.

## Running an example

```bash
# Hermetic (no API keys needed).
uv run python examples/03_conditional_branches.py
uv run python examples/04_native_tool.py
uv run python examples/05_memory.py
uv run python examples/13_dev_server.py
uv run python examples/21_policy_block.py
uv run python examples/22_budget_cap.py
uv run python examples/23_secrets_and_sandbox.py
uv run python examples/24_audit_query.py
uv run python examples/25_webhook_delivery.py
uv run python examples/26_governance_walkthrough.py
uv run python examples/30_contracts_and_mappings.py
uv run python examples/31_guardrails.py
uv run python examples/32_observability.py
uv run python examples/33_mcp_tools.py

# Needs OPENAI_API_KEY (real LiteLLM calls).
OPENAI_API_KEY=sk-... uv run python examples/00_hello.py
OPENAI_API_KEY=sk-... uv run python examples/01_first_graph.py
OPENAI_API_KEY=sk-... uv run python examples/02_multi_agent.py
OPENAI_API_KEY=sk-... uv run python examples/10_serve_in_python.py

# HTTP flow (starts a real uvicorn on :8021, no API key required).
uv run python examples/20_approval_gate.py
```

Every example exits cleanly with a summary of what happened. Examples
that need an API key print `SKIP: ...` and exit `0` when the key is
missing, so the suite stays safe to run in forked-PR CI.

## Why the rewrite

Before this set the `examples/` folder had ~14 files that redefined
stub runners, called `litellm.completion` directly, referenced internal
planning docs in docstrings, and skipped the library's own agent
runtime. Several claimed to teach things they didn't (the old
`agent_handoff.py` never actually handed off in-graph; the old
`attach_memory.py` never attached memory to an agent). The service
path — how you turn a graph into a running API — wasn't shown at all.

This rewrite uses only `zeroth.core.*` types you'd use in production:
`AgentRunner`, `LiteLLMProviderAdapter`, `ExecutableUnitRunner`,
`NativeUnitManifest`, `MemoryConnectorResolver`, `PolicyGuard`,
`BudgetEnforcer`, `WebhookDeliveryWorker`, `bootstrap_service`,
`create_app`, and so on. If you're learning the library, read them
top-down. If you're porting a graph into production, `10`, `11`, and
`12` are the files you need.
