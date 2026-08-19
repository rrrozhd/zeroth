# Deploy the LangGraph release

This is the canonical clean install and operations path for Zeroth `0.23.9.19`.
The tested compatibility matrix is LangGraph `1.2.9`, Agent Server `0.11.1`,
and Zeroth adapter `1.0`.

!!! warning
    **Gateway-only mode cannot enforce internal Agent Server tool calls.** The
    gateway enforces admission and records observed traffic. Tool-body allow,
    deny, and approval enforcement requires the in-process adapter.

## Clean install

Use Python 3.12 and install only the deployment surface you operate:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "zeroth-core[langgraph,langgraph-gateway]==0.23.9.19"
```

For managed Agent Server deployments, put the Zeroth gateway in front of the
managed endpoint, configure the upstream URL and authentication, and install
`ZerothMiddleware` or `govern_tools` inside the application when enforced tool
calls are required. Managed availability does not change the gateway-only limit.

For self-hosted deployment, build the release image and use Compose:

```bash
export ZEROTH_SERVICE_API_KEYS_JSON='[{"credential_id":"release-smoke","secret":"release-smoke-key","subject":"release-operator","roles":["operator"]}]'
export SIGNING_DEPLOYMENT="$(python -c 'import secrets; print(secrets.token_hex(32))')"
uv build --wheel
docker compose build
docker compose run --rm zeroth zeroth-core seed-demo
docker compose up --wait
python release/langgraph/harness.py smoke --require-gateway
python release/langgraph/harness.py gateway-smoke --api-key release-smoke-key
```

The Compose file includes a bounded, test-only Agent Server fixture so this
release smoke is deterministic and needs no proprietary Agent Server image. For
a real self-hosted or managed upstream, set `ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_URL`
and `ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_AUDIENCE`; keep the gateway deployment ref
equal to the seeded or imported deployment.

The image runs as UID `10001`; `/health/ready` checks configured dependencies.
Invalid environment configuration fails startup. Compose gives shutdown 30
seconds so the existing runtime drain can finish in-flight work and flush audit
delivery before exit.

## Environment variables

| Variable | Purpose |
|---|---|
| `ZEROTH_LANGGRAPH_GATEWAY__ENABLED` | Enable the Agent Server gateway. |
| `ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_URL` | Managed or self-hosted Agent Server URL. |
| `ZEROTH_DATABASE__BACKEND` | `sqlite` or `postgres`. |
| `ZEROTH_DATABASE__POSTGRES_DSN` | Postgres connection string for self-hosted use. |
| `ZEROTH_SERVICE_API_KEYS_JSON` | Service credentials; keep secrets outside manifests. |

See the generated configuration reference for the full canonical names and
defaults. Allocate at least 1 CPU and 1 GiB memory for the gateway, then size
workers and connection pools from measured concurrency; the synthetic benchmark
does not predict model or network resources.

## Interrupts, retries, and outages

Approval interrupts need a durable LangGraph checkpointer and stable
`thread_id`. Resume only through the original thread. The approval lifecycle
uses a claim fence and argument fingerprint for idempotency: retries cannot run
the approved tool body more than once. Arbitrary interrupts that do not use the
Zeroth approval payload and durable coordinator are outside the supported
resume contract.

Upstream outages and unsupported compatibility fail gateway admission closed;
readiness reports the dependency state. Audit delivery is bounded and drains on
shutdown. Never log request bodies, approval arguments, credentials, or upstream
error text: Zeroth redaction preserves fixed reason codes and compacts terminal
approval arguments.

## Release evidence

Run the real governance demo, repeated benchmark, and fail-closed checklist:

```bash
python examples/27_langgraph_release.py --json
python release/langgraph/harness.py benchmark --samples 20 --output /tmp/benchmark.json
python release/langgraph/harness.py validate --phase source --manifest release/langgraph/release-manifest.json
# After the image, JUnit, SPDX, package inventory, attestation bundle, and
# verification receipt exist:
python release/langgraph/harness.py validate --phase final --manifest release/langgraph/release-manifest.json
```

`release-manifest.json` is validated, not followed. The set of artifacts the gate
demands is hardcoded in `release/langgraph/release_evidence.py`; the manifest is a
committed declaration that must agree with it, so editing the manifest can only
cause a failure and can never change what is checked. That is the point — a
manifest that drove resolution would let a candidate choose which evidence it is
judged on. Adding a new artifact means changing `REQUIRED_EVIDENCE` and the
manifest together.

Final validation is the CLI default and the only publishable checklist result;
`--phase source` is an explicit pre-build check of committed evidence only. The
final gate cross-checks the expected release tests, installed image packages,
image-bound SPDX document, and the GitHub CLI verification receipt for the
exported image archive instead of accepting unrelated files with similar shapes.

The benchmark executes one deterministic public `govern_tools` `StateGraph`
locally and through `HttpToolDecisionClient` over a real loopback HTTP sidecar.
Its report records the sample distribution, hardware, variance, ordering, a
measured `0.16.1.7` baseline, and derived thresholds. It excludes model latency,
an external network, and the Agent Server runtime.
