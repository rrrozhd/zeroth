# Vendor Due-Diligence Desk (`vendor-dd`)

A production-shaped reference application built on Zeroth: an API service that
takes a vendor dossier and produces a governed third-party risk assessment —
screened, scored, human-approved when risky, cost-metered, and fully auditable.

This is the "business-shaped reference app" the product needs: it is a real
workload (third-party / vendor risk review is a standing process in any
regulated company), and it deliberately exercises **every** runtime surface the
platform has, so it doubles as the platform's living integration proof.

## Why this domain

Vendor due-diligence is the workload the pitch is about: decisions that need an
audit trail ("whose decision was this"), human sign-off on risky outcomes,
grounding in internal policy documents, deterministic computation next to LLM
judgment, and per-tenant cost control. Nothing here is demo garnish — the
governance features are the product requirements.

## The flow

```
                                 ┌──────────────────────┐
                                 │  policy corpus (RAG)  │
                                 └──────────┬───────────┘
                                            │
 intake ──► normalize ──► policy-context ──► screen ──► financial-metrics ──► prepare-panel
 (entry)    (INLINE        (retrieval)      (agent +     (PROJECT unit,        (NATIVE unit,
             unit)                           tool edge     built archive)       parallel fan-out)
                                             to NATIVE                              │
                                             sanctions                    ┌─────────┼─────────┐
                                             tool)                        ▼         ▼         ▼
                                                                      dimension-panel (SUBGRAPH ×N)
                                                                      child graph: vendor-dd-dimension
                                                                          │  (collect fan-in)
                                                                          ▼
                                                                      risk-score (INLINE unit)
                                                                       │            │
                                                       tier ∈ {high,critical}     else
                                                                       ▼            │
                                                                 risk-review        │
                                                                 (HUMAN APPROVAL)   │
                                                                       └─────┬──────┘
                                                                             ▼
                                                                       report (agent,
                                                                       thread write)
                                                                             ▼
                                                                       report-stamp
                                                                       (WRAPPED_COMMAND:
                                                                        openssl sha256)
                                                                             ▼
                                                                     webhook delivery
```

Plus a second deployment, **`vendor-dd-chat`**: a message-list agent
(`input_messages_key` + `persist_conversation`) that answers follow-up
questions about a completed assessment on the same `thread_id` — persistent
conversations across runs.

## Coverage matrix

| Platform surface | Where it is exercised |
|---|---|
| EntrypointNode + public input contract | `intake` (main), `dim-intake` (child), `chat-intake` (chat) |
| AgentNode | `screen`, `report`, `dim-analyst` (child), `chat-analyst` |
| ExecutableUnitNode — INLINE | `normalize`, `risk-score` (authored source, sandboxed subprocess, content-addressed) |
| ExecutableUnitNode — NATIVE | `sanctions-screen` (tool-attached), `prepare-panel` |
| ExecutableUnitNode — PROJECT | `financial-metrics` (archive + build step) |
| ExecutableUnitNode — WRAPPED_COMMAND | `report-stamp` (`openssl dgst -sha256` over the report) |
| Tool edges (`kind="tool"`) + AgentToolBinding | `screen` → `sanctions-screen` |
| RetrievalNode (RAG) | `policy-context` over the seeded policy corpus |
| SubgraphNode | `dimension-panel` → published child graph `vendor-dd-dimension` |
| Parallel fan-out/fan-in (`parallel_config`) | `prepare-panel` splits `panel[]`, subgraph runs per branch, `collect` fan-in |
| Conditional edges | `risk-score` → `risk-review` vs direct-to-`report` on risk tier |
| HumanApprovalNode | `risk-review`, resolved over HTTP |
| EdgeMapping operations | passthrough / rename / constant / default across the main graph's edges |
| Memory connectors + template memory bindings | report context injection; chat thread memory |
| Threads / persistent conversations | `vendor-dd-chat` (`persist_conversation`, same `thread_id` across runs) |
| Policy / capability bindings | `policy://vendor-dd/sandboxed-units` bound on every sandboxed unit node; guard evaluation lands in the audit records (`enforcement_applied`) |
| Budget caps (bundled econ plane) | low-cap tenant trips enforcement in the runbook |
| Per-node cost metering | verified against the econ plane after runs |
| Audit trail + hash chain + evidence | `verify.py`: per-node records, chain verification, evidence export |
| Webhooks (HMAC, retry) | `run.completed` delivered to a local receiver in the runbook |
| Guardrails / dead-letter | failure lane in the runbook |

## Files

| File | Purpose |
|---|---|
| `contracts.py` | Every Pydantic contract, registered under `contract://vendor-dd/*` |
| `units.py` | Native callables, inline sources, wrapped-command + project manifests, registry builder |
| `project_unit/` | Source tree for the PROJECT-mode unit (`finmetrics`), archived + built at seed time |
| `graphs.py` | Builders for the three graphs (main, dimension child, chat) |
| `providers.py` | `ScriptedProviderAdapter` (hermetic) / LiteLLM (real) selection |
| `seed.py` | One-shot: migrations → contracts → corpus ingestion → graphs → publish → deploy ×3 |
| `entrypoint.py` | Production service entrypoint (runner factory + unit registry + auth + econ) |
| `runbook.py` | Drives the scenarios end-to-end over HTTP |
| `verify.py` | Asserts the coverage matrix actually happened (audits, costs, chain, unit modes) |
| `fixtures/` | Vendor dossiers + the internal policy corpus |

## Scenarios (runbook)

1. **Clean vendor, auto lane** — low risk, no approval, report delivered by webhook.
2. **Sanctioned vendor, approval lane** — screening tool flags, high tier, run pauses
   `WAITING_APPROVAL`, reviewer resolves over HTTP, run completes.
3. **Budget trip** — tenant with a low cap; enforcement blocks before the provider call.
4. **Follow-up chat** — two `vendor-dd-chat` runs on one `thread_id`; the second
   turn provably remembers the first.
5. **Verification pass** — `verify.py` checks per-node audits, chain integrity,
   cost events, and that all four execution-unit modes actually ran.

## Running

```bash
# 1. Seed (fresh SQLite): contracts, corpus, graphs, deployments
uv run python -m apps.vendor_dd.seed

# 2. Serve
ZEROTH_DEPLOYMENT_REF=vendor-dd uv run python -m apps.vendor_dd.entrypoint

# 3. Drive the scenarios (separate terminal)
uv run python -m apps.vendor_dd.runbook

# 4. Verify coverage
uv run python -m apps.vendor_dd.verify
```

Hermetic by default (scripted provider, no keys needed). Set `OPENAI_API_KEY`
(or any LiteLLM-supported key) and `VENDOR_DD_REAL_LLM=1` to run the agents
against a real model.
