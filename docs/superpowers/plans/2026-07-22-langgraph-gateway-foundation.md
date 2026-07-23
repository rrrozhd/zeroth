# LangGraph Gateway Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Agent Server-compatible Zeroth gateway that authenticates and admits LangGraph runs, injects signed reserved context, transparently proxies the approved L1 endpoint baseline, reports only evidence-backed capabilities, and proves direct-versus-proxied equivalence.

**Architecture:** The gateway is an optional mode of the existing FastAPI service. A small `zeroth.core.langgraph_gateway` package owns endpoint classification, compatibility detection, admission, signed context mutation, streaming HTTP/WebSocket forwarding, correlation/audit events, and capability status; existing service authentication, signing, policy, budget, secrets, audit, and correlation objects are injected at bootstrap. Governance-aware JSON requests are bounded and rewritten before forwarding, while all responses and non-governance request bodies remain streaming so upstream status, payload bytes, SSE framing, backpressure, cancellation, and disconnect semantics stay intact.

**Tech Stack:** Python 3.12, FastAPI/Starlette ASGI, httpx, websockets, Pydantic v2, pytest/pytest-asyncio, LangGraph 1.2.9, langgraph-api 0.11.1, langgraph-sdk, RemoteGraph.

---

## Scope boundaries

This plan implements only Gateway Foundation. It deliberately does **not** implement `govern_graph`, callback collection, OpenTelemetry mapping/export, governed tool wrappers, middleware, tool decision APIs, structured approval interrupts, approval reconciliation, or upstream approval resume. It defines `CapabilityEvidenceProvider`, `GatewayEventSink`, and correlation/evidence models that those later phases can implement without changing the proxy contract.

The initial compatibility claim is exactly `langgraph==1.2.9` with `langgraph-api==0.11.1`, both the approved design baseline and the newest stable releases verified on 2026-07-22. Unknown versions or OpenAPI fingerprints are `unsupported`; they are never inferred compatible from a successful request. Protocol v2 covers HTTP commands and POST event streams in this phase. The optional WebSocket event-stream route is transparently forwarded and tested, but governance is applied only to the in-band `run.start` and `input.respond` command envelopes; other v2 messages pass through unchanged.

The claimed inventory is explicit and generated into pinned conformance cases from the `0.11.1` OpenAPI fixture:

| Group | Method and path | Foundation behavior |
|---|---|---|
| System | `GET /ok`, `GET /info`, `GET /openapi.json` | Transparent; compatibility detection |
| Assistants | `POST /assistants`, `GET /assistants/{assistant_id}`, `GET /assistants/{assistant_id}/graph`, `POST /assistants/search` | Transparent |
| Threads | `POST /threads`, `GET /threads/{thread_id}`, `POST /threads/search`, `GET /threads/{thread_id}/stream` | Transparent |
| State/history | `GET /threads/{thread_id}/state`, `POST /threads/{thread_id}/state`, `POST /threads/{thread_id}/state/checkpoint`, `POST /threads/{thread_id}/history` | Transparent |
| Threaded runs | `POST /threads/{thread_id}/runs`, `POST /threads/{thread_id}/runs/stream`, `POST /threads/{thread_id}/runs/wait` | Governed admission + signed context |
| Stateless runs | `POST /runs/stream`, `POST /runs/wait` | Governed; approvals remain unsupported |
| Run lifecycle | `GET /threads/{thread_id}/runs/{run_id}`, `GET /threads/{thread_id}/runs/{run_id}/join`, `GET /threads/{thread_id}/runs/{run_id}/stream`, `POST /threads/{thread_id}/runs/{run_id}/cancel` | Transparent |
| Protocol v2 | `POST /threads/{thread_id}/commands` | `run.start` and `input.respond` governed; known read-only methods transparent; unknown methods denied |
| Protocol v2 events | `POST /threads/{thread_id}/stream/events`, `WS /threads/{thread_id}/stream/events` | Transparent stream; in-band WS run-creating commands governed |

Crons, A2A, MCP, Store, arbitrary custom routes, and every endpoint absent from this table are outside the compatibility claim. The table must match the pinned OpenAPI projection; if a listed route differs in `0.11.1`, correct the plan and fixture rather than approximating it at runtime.

## File and responsibility map

### New production files

- `src/zeroth/core/langgraph_gateway/__init__.py` — narrow public exports.
- `src/zeroth/core/langgraph_gateway/models.py` — route, admission, compatibility, correlation, capability, and stable `zeroth.*` error models.
- `src/zeroth/core/langgraph_gateway/inventory.py` — method/path inventory and body-dependent protocol-v2 classification.
- `src/zeroth/core/langgraph_gateway/context.py` — `_zeroth` removal, canonical signed envelope creation/verification, and JSON request mutation.
- `src/zeroth/core/langgraph_gateway/admission.py` — admission orchestrator and injectable classifier/budget interfaces.
- `src/zeroth/core/langgraph_gateway/compatibility.py` — server-info/OpenAPI detection and exact-version/fingerprint matrix.
- `src/zeroth/core/langgraph_gateway/capabilities.py` — conservative per-deployment/per-run reporting interfaces; foundation implementation returns `admission` only.
- `src/zeroth/core/langgraph_gateway/headers.py` — hop-by-hop filtering and configured upstream credential replacement.
- `src/zeroth/core/langgraph_gateway/transport.py` — long-lived httpx transport and WebSocket duplex forwarding.
- `src/zeroth/core/langgraph_gateway/events.py` — incremental correlation extraction and existing-audit-repository sink.
- `src/zeroth/core/langgraph_gateway/proxy.py` — request pipeline, response streaming, disconnect cleanup, and Zeroth-origin error mapping.
- `src/zeroth/core/langgraph_gateway/routes.py` — catch-all HTTP and protocol-v2 WebSocket route registration.

### Modified production files

- `src/zeroth/core/config/settings.py` — validated `LangGraphGatewaySettings` nested under `ZerothSettings`.
- `src/zeroth/core/policy/models.py` — optional run-admission constraints on the existing policy definition.
- `src/zeroth/core/policy/guard.py` — `evaluate_run_admission` using the existing registry.
- `src/zeroth/core/econ/budget.py` — backward-compatible rich budget status for gateway degradation reporting.
- `src/zeroth/core/service/bootstrap.py` — construct and own gateway dependencies.
- `src/zeroth/core/service/app.py` — register gateway routes after Zeroth-native routes and close transport in lifespan.
- `src/zeroth/core/service/health.py` — add upstream compatibility and conservative capability output.
- `pyproject.toml` / `uv.lock` — WebSocket runtime dependency plus pinned conformance group.

### New tests and fixtures

- `tests/langgraph_gateway/test_settings.py`
- `tests/langgraph_gateway/test_inventory.py`
- `tests/langgraph_gateway/test_context.py`
- `tests/langgraph_gateway/test_admission.py`
- `tests/langgraph_gateway/test_compatibility.py`
- `tests/langgraph_gateway/test_headers.py`
- `tests/langgraph_gateway/test_http_proxy.py`
- `tests/langgraph_gateway/test_websocket_proxy.py`
- `tests/langgraph_gateway/test_events.py`
- `tests/langgraph_gateway/test_health.py`
- `tests/langgraph_gateway/test_resilience.py`
- `tests/test_econ_budget.py`
- `tests/langgraph_gateway/fixtures/capture_openapi.py`
- `tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json`
- `tests/langgraph_gateway/conformance/graph.py`
- `tests/langgraph_gateway/conformance/langgraph.json`
- `tests/langgraph_gateway/conformance/cases.py`
- `tests/langgraph_gateway/conformance/harness.py`
- `tests/langgraph_gateway/conformance/test_agent_server_0_11_1.py`
- `tests/langgraph_gateway/conformance/test_sdk_remote_graph.py`
- `tests/langgraph_gateway/conformance/test_differential.py`
- `tests/langgraph_gateway/conformance/cassettes/deterministic.json`

## Stable contracts used throughout the plan

Implement these shared shapes first and keep later tasks dependent on them rather than passing loose dictionaries:

```python
class GovernanceLevel(StrEnum):
    ADMISSION = "admission"
    OBSERVED = "observed"
    ENFORCED = "enforced"

class RouteDisposition(StrEnum):
    GOVERNED = "governed"
    TRANSPARENT = "transparent"
    UNSUPPORTED = "unsupported"

class GatewayError(BaseModel):
    code: str                  # always starts with "zeroth."
    correlation_id: str
    retryable: bool
    reason: str                # safe, no upstream credential/body content

class AdmissionRequest(BaseModel):
    tenant_id: str
    principal_id: str
    roles: tuple[str, ...]
    deployment_ref: str
    assistant_id: str | None
    thread_id: str | None
    operation: str
    input_payload: Any = Field(exclude=True, repr=False)  # internal classifier input only
    input_classification: str
    input_size_bytes: int
    policy_bindings: tuple[str, ...]

class AdmissionDecision(BaseModel):
    allowed: bool
    policy_version: str
    reason: str | None = None
    budget_spend_usd: float | None = None
    budget_cap_usd: float | None = None
    budget_check_degraded: bool = False

class BudgetCheckResult(BaseModel):
    allowed: bool
    spend_usd: float
    cap_usd: float
    degraded: bool = False
    failure_mode: Literal["none", "fail_open", "fail_closed"] = "none"

class GatewayEventSink(Protocol):
    async def emit(self, event: GatewayEvent) -> None: ...

class CapabilityEvidenceProvider(Protocol):
    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None: ...
```

`CapabilityReporter` must clamp the returned level to the evidence it validates. In this phase the default evidence provider always returns `None`, so every proxied run and the deployment health output report `admission`; there is no endpoint that can self-assert `observed` or `enforced`.

`BudgetCheckResult` is defined in `zeroth.core.econ.budget`, next to `BudgetEnforcer`; the gateway imports that neutral economics contract. The economics package must never import `zeroth.core.langgraph_gateway`. `AdmissionRequest.input_payload` exists only for the injected classifier and is excluded from model dumps, reprs, audit metadata, hashes, and signed context.

### Task 1: Define settings and the exact endpoint inventory

**Files:**
- Create: `src/zeroth/core/langgraph_gateway/__init__.py`
- Create: `src/zeroth/core/langgraph_gateway/models.py`
- Create: `src/zeroth/core/langgraph_gateway/inventory.py`
- Modify: `src/zeroth/core/config/settings.py`
- Test: `tests/langgraph_gateway/test_settings.py`
- Test: `tests/langgraph_gateway/test_inventory.py`
- Create: `tests/langgraph_gateway/fixtures/capture_openapi.py`
- Create: `tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json`

- [x] **Step 1: Write failing settings validation tests**

Cover disabled defaults, required upstream URL/deployment/audience when enabled, absolute HTTP(S) URL validation, positive timeouts/context TTL/body limit, credential header/scheme validation, and `stale_threshold_seconds > 2 * heartbeat_interval_seconds` even though heartbeat evidence is implemented later.

```python
def test_gateway_enabled_requires_upstream_identity():
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(enabled=True)

def test_unknown_routes_default_to_deny():
    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server:8123",
        upstream_audience="agent-server:fixture",
        deployment_ref="external-agent",
    )
    assert settings.unknown_endpoint_mode == "deny"
```

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/langgraph_gateway/test_settings.py`
Expected: FAIL because `LangGraphGatewaySettings` does not exist.

- [x] **Step 3: Implement the settings model and top-level wiring**

Add fields for `enabled`, `upstream_url`, `upstream_audience`, `deployment_ref`, `upstream_credential_ref`, `upstream_credential_header`, `upstream_credential_scheme`, `connect_timeout_seconds`, `read_timeout_seconds`, `write_timeout_seconds`, `pool_timeout_seconds`, `context_ttl_seconds`, `max_governed_body_bytes`, `unknown_endpoint_mode`, `policy_bindings`, `supported_langgraph_versions=("1.2.9",)`, `supported_agent_server_versions=("0.11.1",)`, `heartbeat_interval_seconds=30`, and `stale_threshold_seconds=90`. Add `langgraph_gateway: LangGraphGatewaySettings` to `ZerothSettings`.

- [x] **Step 4: Capture the pinned OpenAPI operation projection**

`capture_openapi.py` uses `importlib.metadata.files("langgraph-api")` to locate the distribution's bundled `openapi.json`, retains only sorted `(method, path, operationId)` rows plus the package version, and writes deterministic JSON. Generate it directly from the pinned distribution:

Run: `uv run --with langgraph-api==0.11.1 python tests/langgraph_gateway/fixtures/capture_openapi.py --output tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json`
Expected: a committed projection with `package_version: "0.11.1"`; a second run produces no diff.

- [x] **Step 5: Write the failing inventory table tests**

Parameterize the complete baseline:

```python
GOVERNED = [
    ("POST", "/threads/t/runs"),
    ("POST", "/threads/t/runs/stream"),
    ("POST", "/threads/t/runs/wait"),
    ("POST", "/runs/stream"),
    ("POST", "/runs/wait"),
]
TRANSPARENT = [
    ("GET", "/threads/t/runs/r"),
    ("GET", "/threads/t/runs/r/join"),
    ("GET", "/threads/t/runs/r/stream"),
    ("POST", "/threads/t/runs/r/cancel"),
    ("GET", "/threads/t/state"),
    ("POST", "/threads/t/state"),
    ("POST", "/threads/t/state/checkpoint"),
    ("POST", "/threads/t/history"),
    ("POST", "/assistants/search"),
    ("POST", "/threads/search"),
]
```

Assert every route in the scope table exists in the committed OpenAPI projection (except the WebSocket upgrade over the event-stream path, which OpenAPI cannot express). Assert `commands` is body-dependent: `run.start` and `input.respond` are governed, read-only commands are transparent, and unknown methods are unsupported.

- [x] **Step 6: Run RED, implement declarative compiled patterns, then run GREEN**

Run: `uv run pytest -q tests/langgraph_gateway/test_inventory.py`
Expected RED: no classifier. Implement immutable `EndpointRule` values with anchored regexes and one `classify_protocol_command` parser; do not use a permissive catch-all for claimed routes.

Run: `uv run pytest -q tests/langgraph_gateway/test_settings.py tests/langgraph_gateway/test_inventory.py`
Expected GREEN: PASS.

- [x] **Step 7: Commit**

```bash
git add src/zeroth/core/langgraph_gateway src/zeroth/core/config/settings.py tests/langgraph_gateway/test_settings.py tests/langgraph_gateway/test_inventory.py tests/langgraph_gateway/fixtures
git commit -m "feat: define LangGraph gateway inventory"
```

### Task 2: Create and verify the signed reserved context

**Files:**
- Create: `src/zeroth/core/langgraph_gateway/context.py`
- Test: `tests/langgraph_gateway/test_context.py`

- [x] **Step 1: Write failing envelope tests**

Use a deterministic clock and `EnvHmacSigner`. Assert claims contain schema version, tenant, principal, sorted roles, deployment, audience, correlation ID, policy version, `iat`, `exp`, and optional content classification. Assert all values are JSON serializable and canonical bytes do not change with input dictionary ordering.

```python
claims = ReservedContextClaims(
    tenant_id="tenant-a",
    principal_id="user-7",
    roles=("operator",),
    deployment_ref="external-agent",
    audience="agent-server:fixture",
    correlation_id="corr-1",
    policy_version="sha256:abc",
    issued_at=100,
    expires_at=160,
)
token = codec.encode(claims)
assert codec.decode(token, audience="agent-server:fixture", deployment_ref="external-agent") == claims
```

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/langgraph_gateway/test_context.py`
Expected: FAIL because the codec is missing.

- [x] **Step 3: Implement the versioned envelope**

Use base64url without padding and a compact `header.payload.signature` representation. The header is `{"alg": signer.algorithm(), "kid": signer.key_id(), "typ": "ZEROTH-RUN-CONTEXT", "v": 1}`. Sign `base64url(header) + b"." + base64url(payload)` with the existing `SigningKeyProvider`; reject `NullSigner`, missing signatures, unknown key IDs, bad signatures, wrong audience/deployment, future `iat`, expired `exp`, or excessive TTL. Verification uses `hmac.compare_digest` through the provider and never logs token contents.

- [x] **Step 4: Write failing request mutation tests**

Cover threaded/stateless run bodies and protocol-v2 `params`. Seed attacker values at top-level `_zeroth`, `context._zeroth`, `metadata._zeroth`, and `config.configurable._zeroth`. Assert all are removed and exactly one gateway token is written at `config.configurable._zeroth`. Preserve callbacks and every unrelated unknown field byte-semantically after JSON decode/encode. Reject non-object JSON and over-limit governed bodies with `zeroth.invalid_request` / `zeroth.request_too_large`.

- [x] **Step 5: Implement `inject_reserved_context`, run GREEN, and commit**

Run: `uv run pytest -q tests/langgraph_gateway/test_context.py`
Expected: PASS.

```bash
git add src/zeroth/core/langgraph_gateway/context.py tests/langgraph_gateway/test_context.py
git commit -m "feat: sign LangGraph gateway context"
```

### Task 3: Extend PolicyGuard for run admission and compose budget checks

**Files:**
- Modify: `src/zeroth/core/policy/models.py`
- Modify: `src/zeroth/core/policy/guard.py`
- Modify: `src/zeroth/core/econ/budget.py`
- Create: `src/zeroth/core/langgraph_gateway/admission.py`
- Test: `tests/policy/test_guard.py`
- Test: `tests/test_econ_budget.py`
- Test: `tests/langgraph_gateway/test_admission.py`

- [x] **Step 1: Add failing backward-compatibility and admission-policy tests**

Existing node policy tests must remain unchanged. Add optional `allowed_tenants`, `allowed_principals`, `required_roles`, `allowed_assistants`, `allowed_deployments`, `allowed_input_classifications`, and `max_input_bytes` fields to `PolicyDefinition`, all empty/`None` by default. Test that old policies serialize identically except for default-excluded new fields and that multiple bindings combine by intersection/strictest limit.

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/policy/test_guard.py -k 'admission or backwards'`
Expected: FAIL because run admission constraints and evaluation do not exist.

- [x] **Step 3: Implement `PolicyGuard.evaluate_run_admission`**

The method accepts `AdmissionRequest`, resolves only its declared `policy_bindings` from the existing `PolicyRegistry`, denies missing required role or any value outside a non-empty allow-list, applies the lowest configured input-size ceiling, and returns a stable policy version `sha256:` digest over canonical resolved definitions. A missing binding is a `zeroth.policy_unavailable` failure, not allow. Do not fabricate a Zeroth graph/node/run to call the node-oriented evaluator.

- [x] **Step 4: Write failing rich budget-status compatibility tests**

Keep `BudgetEnforcer.check_budget()` returning the existing `(allowed, spend, cap)` tuple for every current caller. Add `check_budget_status()` returning `BudgetCheckResult`: successful capped/unlimited responses use `degraded=False`; caught backend failures use `degraded=True` and `failure_mode="fail_open"|"fail_closed"`. Both methods share one internal check so they cannot disagree or make duplicate backend calls.

Run: `uv run pytest -q tests/test_econ_budget.py -k 'status or fail'`
Expected RED: `check_budget_status` does not exist.

- [x] **Step 5: Implement the backward-compatible budget API**

Move the existing cache/request/error logic into `_check_budget_status`. `check_budget_status` returns the rich model; `check_budget` projects it to the legacy tuple. Cache successful results only and include `degraded=False` in cached status. Never infer degradation from `(True, 0.0, inf)` because that is also a legitimate unlimited budget.

- [x] **Step 6: Write failing admission orchestration tests**

Use recording policy guard, classifier, and rich budget fakes. Prove order: classification -> policy -> budget -> context decision. A policy deny never calls budget; a budget deny returns before transport is touched; allowed requests carry spend/cap into audit metadata. Test configured BudgetEnforcer fail-open as `allowed=True, budget_check_degraded=True` and fail-closed as a denial. The orchestrator does not change `BudgetEnforcer`'s configured outage posture.

- [x] **Step 7: Implement minimal protocols and orchestrator**

```python
class InputClassifier(Protocol):
    async def classify(self, payload: object) -> str: ...

class BudgetChecker(Protocol):
    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult: ...

async def admit(request, *, policy_guard, budget_checker, classifier) -> AdmissionDecision:
    classification = await classifier.classify(request.input_payload)
    policy = policy_guard.evaluate_run_admission(request.with_classification(classification))
    if not policy.allowed:
        return policy
    # check budget only after policy allow; annotate, never weaken a deny
```

Default classification is `unclassified`; deployments that deny unclassified inputs must inject a real classifier in a later policy package.

- [x] **Step 8: Run GREEN and commit**

Run: `uv run pytest -q tests/policy/test_guard.py tests/test_econ_budget.py tests/langgraph_gateway/test_admission.py`
Expected: PASS.

```bash
git add src/zeroth/core/policy src/zeroth/core/econ/budget.py src/zeroth/core/langgraph_gateway/admission.py tests/policy/test_guard.py tests/test_econ_budget.py tests/langgraph_gateway/test_admission.py
git commit -m "feat: admit LangGraph runs by policy and budget"
```

### Task 4: Detect upstream compatibility and report capabilities conservatively

**Files:**
- Create: `src/zeroth/core/langgraph_gateway/compatibility.py`
- Create: `src/zeroth/core/langgraph_gateway/capabilities.py`
- Test: `tests/langgraph_gateway/test_compatibility.py`

- [x] **Step 1: Write failing exact-version detection tests**

Use `httpx.MockTransport` for: exact `0.11.1` server info + expected OpenAPI fingerprint, version-only exact match, managed server with no version but known fingerprint, `0.11.2` unknown patch, malformed info, changed OpenAPI shape, and outage. Only the first three are supported. Normalize OpenAPI by sorting method/path/operation IDs and hashing that projection, not descriptions or examples.

- [x] **Step 2: Implement the compatibility matrix**

`CompatibilityDetector.detect()` probes `/info`, `/ok`, then `/openapi.json` with short bounded timeouts. Return `tested_versions`, `detected_version`, `openapi_fingerprint`, `status` (`supported|unsupported|unavailable`), and a safe reason. Do not retry indefinitely during startup and do not infer a package version from arbitrary response headers.

- [x] **Step 3: Write failing capability tests**

Assert no evidence => `admission`; a heartbeat without run attestation => run remains `admission`; partial tool manifest => at most `observed`; stale evidence => deployment `admission`; invalid signature/mismatched graph version => lower level. These are model/validation seam tests only—no callback, tool, heartbeat endpoint, or persistence implementation.

- [x] **Step 4: Implement clamped reporting, run GREEN, and commit**

Run: `uv run pytest -q tests/langgraph_gateway/test_compatibility.py`
Expected: PASS.

```bash
git add src/zeroth/core/langgraph_gateway/compatibility.py src/zeroth/core/langgraph_gateway/capabilities.py tests/langgraph_gateway/test_compatibility.py
git commit -m "feat: report LangGraph compatibility and capability"
```

### Task 5: Preserve headers, credentials, and raw HTTP streaming

**Files:**
- Create: `src/zeroth/core/langgraph_gateway/headers.py`
- Create: `src/zeroth/core/langgraph_gateway/transport.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/langgraph_gateway/test_headers.py`
- Test: `tests/langgraph_gateway/test_http_proxy.py`

- [x] **Step 1: Write failing header tests**

Verify request/response hop-by-hop headers named by RFC connection semantics are removed, repeated end-to-end headers survive, `Host` is rebuilt for upstream, and client `Authorization`/`X-API-Key` never reach upstream. Resolve the deployment-scoped credential through `resolve_secret_async` and insert only the configured upstream header/scheme. A missing configured credential fails `zeroth.upstream_credential_unavailable` before connecting.

- [x] **Step 2: Implement pure header helpers and run GREEN**

Run: `uv run pytest -q tests/langgraph_gateway/test_headers.py`
Expected: PASS.

- [x] **Step 3: Write failing byte-transparency tests**

Start an in-process upstream ASGI fixture that returns JSON, binary data, 204, repeated headers, 422, 500, and SSE chunks split at hostile byte boundaries. Assert exact status, ordered body bytes, content type, and end-to-end headers. Assert upstream 4xx/5xx bodies are never wrapped in `GatewayError`.

- [x] **Step 4: Implement the long-lived streaming transport**

Construct one `httpx.AsyncClient(http2=True, follow_redirects=False)` per gateway with explicit connect/read/write/pool timeouts and limits. Use `client.send(request, stream=True)`. Response bodies are `aiter_raw()` generators whose `finally` always calls `response.aclose()`. Transparent request bodies use `request.stream()`; only governed JSON bodies are buffered by the earlier size-limited mutator. Never call `.json()`, `.aread()`, or `StreamingResponse(content=bytes)` on upstream responses.

- [x] **Step 5: Add the WebSocket dependency and run GREEN**

Add `websockets>=15,<16` to a `langgraph-gateway` optional extra and the conformance group to development sync. Regenerate lock with `uv lock`.

Run: `uv run pytest -q tests/langgraph_gateway/test_headers.py tests/langgraph_gateway/test_http_proxy.py`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/zeroth/core/langgraph_gateway/headers.py src/zeroth/core/langgraph_gateway/transport.py tests/langgraph_gateway/test_headers.py tests/langgraph_gateway/test_http_proxy.py
git commit -m "feat: stream Agent Server HTTP transparently"
```

### Task 6: Assemble the governed proxy pipeline and stable errors

**Files:**
- Create: `src/zeroth/core/langgraph_gateway/proxy.py`
- Create: `src/zeroth/core/langgraph_gateway/events.py`
- Test: `tests/langgraph_gateway/test_events.py`
- Test: `tests/langgraph_gateway/test_http_proxy.py`

- [x] **Step 1: Write failing pipeline-order tests**

With recording fakes, assert: route classification -> principal resolution -> bounded body parse -> admission -> signed injection -> upstream credential replacement -> transport. An admission deny, invalid JSON, oversized body, unsupported endpoint, incompatible upstream, missing signer, or missing credential must leave the upstream call count at zero.

- [x] **Step 2: Write failing Zeroth error-envelope tests**

Map only gateway-origin failures to `{code, correlation_id, retryable, reason}` with documented status codes: authentication remains existing 401 middleware; policy/budget deny 403; invalid body 400; too large 413; unsupported endpoint/version 501; upstream connect/timeout 502/504; gateway misconfiguration 503. Safe reasons cannot include request values, tokens, secret refs, or upstream response bodies.

- [x] **Step 3: Implement `GatewayProxy.handle_http`**

Set `X-Correlation-ID` on every response. For allowed governed requests, build claims from `current_principal(request)`, the configured deployment/audience, the admission policy version, and the current correlation ID. Preserve upstream successes/errors exactly except for the added correlation header. Respect `unknown_endpoint_mode`: default deny; optional `pass_ungoverned` adds `X-Zeroth-Governance: ungoverned` and emits a warning audit event.

Also set `X-Zeroth-Governance-Level: admission` on governed run responses. This header and the terminal `langgraph.gateway` audit record are the foundation's per-run capability surfaces; upstream response bodies stay byte-transparent. A later evidence provider may raise the reported value only when valid run evidence is available before response headers are committed.

- [x] **Step 4: Write failing correlation/audit tests**

Incrementally observe a tee without delaying downstream delivery. Extract `run_id`, `thread_id`, and `assistant_id` from JSON and complete SSE `data:` frames only; malformed/oversized observation frames disable extraction but continue streaming. Emit a terminal `GatewayEvent` for success, upstream error, client disconnect, gateway denial, and cancellation. The existing audit sink writes a `NodeAuditRecord(node_id="langgraph.gateway", actor=principal.to_actor())` containing IDs, operation, policy/budget result, governance level, compatibility fingerprint, status, timings, and hashes/sizes—not raw input/output.

- [x] **Step 5: Implement best-effort observation**

`TeeObserver.observe(chunk)` must be synchronous and bounded. `GatewayEventSink.emit` failures are logged and counted but never fail or reorder a healthy upstream response. A finalizer runs on normal exhaustion, error, cancellation, and generator close.

- [x] **Step 6: Run GREEN and commit**

Run: `uv run pytest -q tests/langgraph_gateway/test_events.py tests/langgraph_gateway/test_http_proxy.py`
Expected: PASS.

```bash
git add src/zeroth/core/langgraph_gateway/proxy.py src/zeroth/core/langgraph_gateway/events.py tests/langgraph_gateway/test_events.py tests/langgraph_gateway/test_http_proxy.py
git commit -m "feat: govern and correlate proxied LangGraph runs"
```

### Task 7: Forward protocol-v2 WebSockets with bounded backpressure

**Files:**
- Modify: `src/zeroth/core/langgraph_gateway/transport.py`
- Create: `src/zeroth/core/langgraph_gateway/routes.py`
- Test: `tests/langgraph_gateway/test_websocket_proxy.py`

- [x] **Step 1: Write failing duplex tests**

Use a real ephemeral WebSocket upstream. Assert subprotocol selection, text/binary frames, order, close codes/reasons, ping/pong liveness, upstream-first close, client-first close, and abrupt disconnect propagation. For in-band `run.start`/`input.respond`, assert admission and signed context mutation before send; non-run protocol messages are byte-identical.

The gateway route authenticates WebSocket headers with the existing `ServiceAuthenticator` **before** `accept()`, sets `websocket.state.principal`, and creates/sets the correlation ID because `@app.middleware("http")` never sees WebSocket scopes. Missing or invalid client credentials close with documented code 4401 before any upstream connection; client credentials are never forwarded upstream.

- [x] **Step 2: Write the slow-consumer backpressure test**

Have upstream publish numbered frames faster than the client reads. Instrument maximum buffered frames and assert it never exceeds `websocket_queue_size` (default 16); no frame is reordered or silently dropped. Cancellation of either pump cancels its sibling and closes both sockets.

- [x] **Step 3: Implement the duplex pump**

Use two bounded `asyncio.Queue`s and one `TaskGroup` for client->upstream and upstream->client. Do not spawn detached tasks. Read the authenticated identity only from `websocket.state.principal`; never accept identity claims from an in-band command. Resolve upstream credentials during handshake, strip client credentials, preserve allowed subprotocols, and map only gateway handshake failures to the stable envelope via WebSocket close reason/code documented in tests.

- [x] **Step 4: Run GREEN and commit**

Run: `uv run pytest -q tests/langgraph_gateway/test_websocket_proxy.py`
Expected: PASS.

```bash
git add src/zeroth/core/langgraph_gateway/transport.py src/zeroth/core/langgraph_gateway/routes.py tests/langgraph_gateway/test_websocket_proxy.py
git commit -m "feat: proxy LangGraph protocol streams"
```

### Task 8: Wire gateway mode into the existing service and health surface

**Files:**
- Modify: `src/zeroth/core/service/bootstrap.py`
- Modify: `src/zeroth/core/service/app.py`
- Modify: `src/zeroth/core/service/health.py`
- Test: `tests/service/test_app.py`
- Test: `tests/langgraph_gateway/test_health.py`

- [x] **Step 1: Write failing bootstrap ownership tests**

When disabled, no client/probes/routes are constructed. When enabled, `ServiceBootstrap` owns one gateway proxy, transport, detector result, and capability reporter using the already-created authenticator, secret provider, signer, policy guard, budget enforcer, audit repository, and deployment identity. Assert the httpx client closes exactly once during lifespan, including startup failure.

- [x] **Step 2: Implement bootstrap construction**

Create gateway dependencies only after the shared secret provider and signer exist. Enabled gateway startup fails closed if signing is unavailable. Run one bounded compatibility detection; keep the service alive with health `unsupported`/`unavailable`, but reject proxied requests until supported. Do not instantiate callback, tool, or approval components.

- [x] **Step 3: Write failing route-precedence tests**

Zeroth-native `/health`, `/health/live`, `/health/ready`, `/v1/*`, `/console*`, and `/regulus*` routes must win. Gateway routes expose root Agent Server paths such as `/threads`, `/assistants`, `/runs`, `/info`, and protocol stream paths only when enabled. Authentication middleware still resolves the Zeroth principal before proxy code.

- [x] **Step 4: Register routes and extend health output**

`/health/ready` adds `agent_server`; the deployment health payload adds:

```json
{
  "langgraph_gateway": {
    "enabled": true,
    "governance_level": "admission",
    "limitation": "internal tool calls are not enforced in gateway-only mode",
    "compatibility": {
      "tested_langgraph": ["1.2.9"],
      "tested_agent_server": ["0.11.1"],
      "detected_agent_server": "0.11.1",
      "status": "supported",
      "openapi_fingerprint": "sha256:..."
    }
  }
}
```

- [x] **Step 5: Run GREEN and commit**

Run: `uv run pytest -q tests/service/test_app.py tests/test_health_probes.py tests/langgraph_gateway/test_health.py`
Expected: PASS.

```bash
git add src/zeroth/core/service src/zeroth/core/langgraph_gateway/routes.py tests/service/test_app.py tests/test_health_probes.py tests/langgraph_gateway/test_health.py
git commit -m "feat: mount LangGraph gateway service mode"
```

### Task 9: Build the deterministic Agent Server conformance fixture

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/langgraph_gateway/conformance/graph.py`
- Create: `tests/langgraph_gateway/conformance/langgraph.json`
- Create: `tests/langgraph_gateway/conformance/cases.py`
- Create: `tests/langgraph_gateway/conformance/cassettes/deterministic.json`
- Create: `tests/langgraph_gateway/conformance/test_agent_server_0_11_1.py`

- [x] **Step 1: Pin the release-gate environment**

Add a `gateway-conformance` dependency group with exact `langgraph==1.2.9`, `langgraph-api==0.11.1`, and the compatible `langgraph-sdk` resolved by those pins. Add a `langgraph_conformance` pytest marker; keep it out of the default fast unit suite only if startup cost is material, but run it in the explicit release command.

- [x] **Step 2: Write the deterministic graph and cassette**

Create a StateGraph with deterministic nodes for echo/update, ordered custom/token-like stream events, a controlled interrupt/resume, a long-running cancellation point, a predictable upstream exception, and a recorded fake tool sequence. No real LLM/network call is permitted; cassette misses fail the test.

- [x] **Step 3: Write the manifest-driven endpoint cases**

Each case declares method, path builder, request builder, expected status/content type, comparison normalizer, governance expectation, and cleanup. Include all inventory operations: assistant/thread create/read/search, state/history, threaded background/wait/stream, stateless wait/stream, get/join/join-stream/cancel, protocol-v2 `run.start`/`input.respond` and POST event stream, interrupt and native resume, auth errors, validation errors, and unsupported groups.

- [x] **Step 4: Run RED against missing fixture behavior, then complete fixture**

Run: `uv run --group gateway-conformance pytest -q -m langgraph_conformance tests/langgraph_gateway/conformance/test_agent_server_0_11_1.py`
Expected first run: FAIL on any incomplete case. Iterate fixture code only until every direct Agent Server case is deterministic and passes.

- [x] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/langgraph_gateway/conformance
git commit -m "test: define Agent Server conformance fixture"
```

### Consolidated release slice: Tasks 10–12

> **Execution update (user-authorized 2026-07-22):** Execute Tasks 10, 11, and 12 in one implementer context and one coherent release slice. Preserve every acceptance criterion below, use focused TDD while implementing, then run one integrated Tasks 9–12 review followed by one final full-suite/lint gate.

### Task 10: Add SDK, RemoteGraph, and direct-versus-proxied differential harnesses

**Files:**
- Create: `tests/langgraph_gateway/conformance/harness.py`
- Create: `tests/langgraph_gateway/conformance/test_sdk_remote_graph.py`
- Create: `tests/langgraph_gateway/conformance/test_differential.py`

- [x] **Step 1: Write failing comparison-report tests**

`DifferentialReport` separates `semantic_divergences` from `expected_governance_additions`. Normalize only generated IDs/timestamps and explicitly declared correlation/governance additions. Never sort ordered streams or tool sequences, coerce status codes, discard errors, or ignore unknown fields.

```python
assert report.semantic_divergences == []
assert report.expected_governance_additions == [
    "response.header.x-correlation-id",
    "response.header.x-zeroth-governance-level",
    "forwarded.config.configurable._zeroth",
    "audit.langgraph.gateway",
]
```

- [x] **Step 2: Implement paired case execution**

For each manifest case, create isolated direct/proxied threads, replay the same cassette/input, capture status, headers, raw ordered chunks, final JSON, thread state, interrupts/resume values, tool sequence, errors, cancellation outcome, and terminal state. Write a human-readable report artifact under pytest's temporary directory on divergence.

- [x] **Step 3: Add official Python SDK parity tests**

Use `langgraph_sdk.get_client` against direct and gateway URLs for assistants, threads, stateful/stateless runs, stream/wait, join/join-stream, and cancel. The only client change is URL/API credential.

- [x] **Step 4: Add RemoteGraph parity tests**

Use `langgraph.pregel.remote.RemoteGraph` for `invoke`, `ainvoke`, `stream`, `astream`, `get_state`, `get_state_history`, and `update_state`. Assert output/state/ordered chunks match direct behavior and no graph source change is required.

- [x] **Step 5: Run GREEN and commit**

Run: `uv run --group gateway-conformance pytest -q -m langgraph_conformance tests/langgraph_gateway/conformance`
Expected: PASS with zero semantic divergences.

```bash
git add tests/langgraph_gateway/conformance
git commit -m "test: prove LangGraph gateway differential parity"
```

### Task 11: Prove cancellation, disconnect, timeout, and backpressure behavior

**Files:**
- Create: `tests/langgraph_gateway/test_resilience.py`
- Modify: `tests/langgraph_gateway/conformance/test_differential.py`

- [x] **Step 1: Add deterministic client-disconnect tests**

Use a socket-level ASGI client so the connection can close mid-SSE rather than merely stop iterating a buffered TestClient response. Assert the upstream response task is cancelled/closed promptly, no detached task remains, the audit event is `disconnected`, and a run configured upstream with `on_disconnect=continue` is not falsely reported cancelled.

- [x] **Step 2: Add explicit cancellation parity tests**

Create a background run, call the documented cancel endpoint through direct and proxy paths, then join/read state. Compare status code, response body, upstream terminal run status, and final state.

- [x] **Step 3: Add slow-reader and slow-writer tests**

Bound the upstream producer lead under a slow downstream reader and bound request upload lead under a slow upstream reader. Assert maximum observed queue/buffer stays within configured limits and resident data does not scale with total stream length.

- [x] **Step 4: Add timeout/error/finalizer tests**

Cover connect timeout, read timeout before headers, read timeout mid-stream, client cancellation during admission, cancellation during upstream connect, audit parser exception, audit sink exception, and app shutdown with active streams. Every path closes response/client resources once and preserves upstream bytes already delivered.

- [x] **Step 5: Run GREEN and commit**

Run: `uv run pytest -q tests/langgraph_gateway/test_resilience.py tests/langgraph_gateway/test_http_proxy.py tests/langgraph_gateway/test_websocket_proxy.py`
Expected: PASS.

Run: `uv run --group gateway-conformance pytest -q -m langgraph_conformance tests/langgraph_gateway/conformance/test_differential.py`
Expected: PASS.

```bash
git add tests/langgraph_gateway/test_resilience.py tests/langgraph_gateway/conformance/test_differential.py
git commit -m "test: harden LangGraph proxy stream lifecycle"
```

### Task 12: Release-gate the foundation without absorbing later phases

**Files:**
- Modify: `docs/superpowers/plans/2026-07-22-langgraph-gateway-foundation.md` only to check completed boxes during execution.

- [x] **Step 1: Audit scope mechanically**

Run: `git diff --name-only <foundation-base>...HEAD`
Expected: only files listed in this plan. Confirm no changes under the existing LangGraph instrumentation adapter, approvals implementation, tool runtime/wrappers, OTel mapper/exporter, or migrations.

Run: `rg -n "govern_graph|govern_tools|ZerothMiddleware|interrupt\(|Command\(resume|wrap_tool_call" src/zeroth/core/langgraph_gateway`
Expected: no implementation matches; interface/docstrings may name later concepts only where necessary.

- [x] **Step 2: Run focused and adjacent tests**

Run: `uv run pytest -q tests/langgraph_gateway tests/policy/test_guard.py tests/service/test_app.py tests/test_health_probes.py tests/service/test_auth_api.py tests/signing/test_signing.py tests/test_econ_budget.py`
Expected: PASS.

- [x] **Step 3: Run the pinned conformance release gate**

Run: `uv run --group gateway-conformance pytest -q -m langgraph_conformance tests/langgraph_gateway/conformance`
Expected: PASS for LangGraph 1.2.9 / Agent Server 0.11.1 with zero semantic divergences.

- [x] **Step 4: Run repository quality gates**

Run: `uv run ruff check src/ tests/langgraph_gateway`
Expected: PASS.

Run: `uv run ruff format --check src/ tests/langgraph_gateway`
Expected: PASS.

Run: `uv run pytest -q`
Expected: PASS.

- [x] **Step 5: Inspect graph impact and coverage per AGENTS.md**

Rebuild the code-review graph, run change detection, inspect affected flows, and query `tests_for` for the new gateway entry points. Resolve any high-risk untested edge before completion.

- [x] **Step 6: Final atomic verification commit if needed**

Only if verification required test/fixture corrections:

```bash
git add <only-the-corrected-foundation-files>
git commit -m "test: verify LangGraph gateway foundation"
```

Do not squash unrelated user work, stage the whole tree, or include pre-existing uncommitted changes.
