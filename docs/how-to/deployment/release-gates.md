# Release gates and evidence

Zeroth promotes a release only when every gate holds against **the exact
candidate being promoted**. Gates emit machine-readable records; a validator
refuses anything missing, stale, incomplete, bound to another build, or
reporting failure; and promotion jobs depend on that validator rather than on
the individual checks.

The gates, their evidence and their identity bindings are declared in
`release/gates/release-gates.json`. That file is the contract — this page
describes who is responsible for satisfying it.

## Why evidence is bound to an identity

A release constant that names the version can drift from the version actually
being built, and evidence then validates itself. Every record therefore carries
a *measured* identity: the commit, the digests of the built artifacts, the
image digest, the configuration used for the deployment smoke, and the resolved
compatibility set. Change any of them and the identity changes, so evidence
gathered for a different commit or a different build is rejected instead of
being silently accepted.

## The gates

| # | Gate | Proves | Runs at |
|---|---|---|---|
| 1 | Source | Lint, docstring coverage, the Python suite, the console unit suite and the frontend API contract | pull request, nightly, release candidate |
| 2 | Package | The sdist and wheel build, install clean, expose every extra, and pass the suite against the installed wheel | nightly, release candidate |
| 3 | LangGraph compatibility | The pinned LangChain/LangGraph/Agent Server matrix conforms and the governed-tool benchmark holds | nightly, release candidate |
| 4 | Untrusted code execution | The sandbox sidecar still refuses what it must: argv handling, hardening, strict-network containment | nightly, release candidate |
| 5 | Security regression | The reviewed tenant-isolation and hostile-execution matrix ran exactly, completed without skips, and its observable evidence contains no credential canary or GitHub token | pull request, nightly, release candidate |
| 6 | Load and recovery | Versioned burst, sustained, soak, overload, and fault profiles preserve capacity, fairness, recovery and every accepted run | nightly, release candidate |
| 7 | Deployment smoke | The image starts, reports ready, serves the gateway, refuses invalid configuration, drains on shutdown, and carries an SBOM and a verified provenance attestation | release candidate |
| 8 | Remote acceptance | The candidate installs from the published index and the published artifact runs end to end | release candidate |
| 9 | Promotion | Every preceding gate validated, and a named human accepted the release | release candidate, manual |

Gates 1–7 are the **candidate** phase and gate TestPyPI. Gates 8–9 are the
**final** phase and gate PyPI.

## Responsibilities by trigger

### Pull request

Pull requests keep the fast checks only: `ci.yml`, `docs.yml`, `examples.yml`,
`langgraph-compatibility.yml` and `verify-extras.yml`. The gate matrix has no
`pull_request` trigger, so opening a PR never pays for a Docker build, an SBOM,
an attestation or a TestPyPI round trip. A test asserts this rather than
trusting the convention.

Within `ci.yml`, the security job runs only the reviewed `pr-critical` tier in
one portable pytest invocation. It produces JUnit and canonical outcome JSON,
but does not claim the distributed proof: Redis, PostgreSQL and Docker-backed
cases belong to the complete `release-candidate` tier.

### Nightly

`release-gates.yml` runs on a schedule and produces gates 1–6, so drift is
found before a release is cut rather than during one. It validates exactly the
gates it produces — a nightly is never blocked by evidence only a release
candidate can generate.

The nightly security job runs the complete `release-candidate` matrix with a
healthy Redis service and the hosted Docker daemon, which the testcontainers
fixtures use for PostgreSQL. Every required node must report passing setup,
call and teardown phases; a skipped node is a failure, including a skip caused
by unavailable Redis, Docker, or PostgreSQL.

Run the same set on demand from the Actions tab (**Release gates** →
**Run workflow**).

### Release candidate

Publishing a GitHub Release runs `release-zeroth-core.yml`. It builds once,
calls the nightly workflow to gather gates 1–6 against **that** build, adds
gates 7–9, and validates:

- `evidence-gate` validates the candidate phase; TestPyPI publication depends
  on it.
- `evidence-gate-final` validates every gate including the promotion signoff;
  PyPI publication depends on it.

Both verdicts are written to the job summary and retained as artifact bundles
for 90 days.

The security record binds the candidate commit and package identity. Its four
independent results are the matrix pytest exit, exact coverage verification,
the no-skips outcome verdict, and the credential scan. CI emits the record and
uploads JUnit, coverage, outcome, scan, and record files even when one result
fails. Missing evidence, a record from another commit or package, an incomplete
matrix, a failed or skipped node, or a leaked canary therefore blocks candidate
promotion through the same evidence validator as every other gate.

### Manual

One piece of evidence has no CI producer: the promotion signoff. Before
promoting to PyPI, a named human records acceptance at
`release/signoff/<version>.md` — for example `release/signoff/0.20.md` — and
commits it on the release tag.

The file must contain both:

- a line beginning `Signed-off-by:` or `Operator:` naming the human, and
- the candidate identity digest, which the release run prints and which you can
  reproduce with
  `python release/gates/cli.py digest --identity release/evidence/candidate-identity-full.json`.

Requiring the digest is what stops a signoff for an earlier build of the same
version from being accepted for this one — existing is not the same as
accepting *this* candidate. Absent, unsigned, or wrongly-bound, the promotion
gate fails and PyPI stays closed.

The evidence manifest — the candidate identity plus the digest of every gate
record — is sealed and attested with `actions/attest` at the end of the release
run, so the evidence itself carries a signature rather than only the image it
describes.

The operator must treat the security matrix as reviewed scope, not as a broad
claim that every future endpoint is already covered. In particular, repository
installations and repository checkouts are currently **absent** public ingress.
The matrix proves that the reviewed public route inventory exposes no such
ingress and that project execution fails closed without a trusted materializer.
Adding a repository-installation endpoint, checkout path, GitHub App ingress,
or trusted materializer invalidates this absence proof. The feature must not be
promoted until the inventory, matrix cases, tenant-isolation tests, and release
evidence are updated to cover its real read/write/execute lifecycle.

## Load and recovery profile

The load gate runs only on the nightly schedule and for an explicit release
candidate; it is intentionally absent from pull requests. The committed
`release/load/profiles-v1.json` is the executable capacity contract:

| Profile | Duration | Scheduled rate | Maximum in flight |
|---|---:|---:|---:|
| Burst | 15 seconds | 12 requests/second | 24 |
| Sustained | 60 seconds | 6 requests/second | 18 |
| Soak | 300 seconds | 3 requests/second | 12 |
| Overload | 30 seconds | 30 requests/second | 48 |

Every profile covers 3 tenants, 2 deployments per tenant, 2 replicas and at
least 3 workers. Requests rotate across deployments configured for the
LangGraph, slow-script, failing-script, approval, artifact and webhook
scenarios. The companion fault observations exercise the real Redis artifact
and persisted webhook-delivery paths; the linked LangGraph gate exercises the
streaming routes. A surface label therefore cannot replace the native product
behavior it names.

The environment is isolated but production-representative: an
`ubuntu-24.04-arm` runner uses a digest-pinned Python 3.12 container limited to
2 CPUs and 8 GiB, with digest-pinned PostgreSQL 17 and Redis 7.4 services. The
real ASGI application and durable workers run inside that boundary; external
network and model-provider latency are excluded. The committed baseline records
the same operating system, architecture, limits and images plus the exact prior
commit and package version. A different environment fails closed.

### Candidate safe envelope

A candidate is inside the safe envelope only when all of these remain true:

- observed throughput is at least 80% of the pinned baseline for every profile;
- p50, p95 and p99 latency, maximum queue depth, CPU, memory and recovery time
  are no more than 150% of their baseline values;
- rejection rate grows by no more than 0.10; tenant and deployment Jain
  fairness remain at least 0.90, while replica and worker fairness remain at
  least 80% of the pinned baseline; no accepted run ID is lost or accepted
  twice;
- overload refusals are only HTTP 429 or 503 and include a positive
  `Retry-After`; cancellation and graceful drain both reach a terminal state;
- PostgreSQL contention, Redis loss, worker loss, service restart, network
  delay and downstream throttling each demonstrate automatic recovery without
  manual data repair.

This envelope is a release regression boundary, not a claim of universal
production capacity. Re-measure after changing the reference runner, database,
Redis topology or workload shape.

### Baseline and fixed thresholds

`release/load/baseline-v1.json` retains the raw numerical distributions behind
the prior release's summaries. Its SHA-256 digest and the threshold literals
are pinned in `release/load/report.py`. Runtime evaluation never derives a new
threshold from a mutable baseline: editing the baseline fails validation.

A legitimate baseline refresh is deliberate: run the full profiles at least
three isolated times against the exact previous release in the pinned capacity
environment, review every raw distribution and hardware field, then update the
combined baseline, pinned digest and independently declared threshold
derivation together. Every source run has a distinct observation digest, and a
candidate observation digest may not overlap those baseline runs. The tests
recompute throughput, p50/p95/p99, rejection, queue, resource and recovery
values from the retained distributions.

### Reproducing the gate

Build the artifacts used by the candidate identity, then run the same pinned
ARM capacity envelope used by the workflow. The service images and the Python
runtime are immutable inputs; replace `load-gate` only with another isolated
network name:

```bash
set -euo pipefail
RUNTIME='python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2'
POSTGRES='postgres:17-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73'
REDIS='redis:7.4-alpine@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2'
docker network create load-gate
docker run -d --rm --platform linux/arm64 --name load-gate-postgres --network load-gate \
  --health-cmd 'pg_isready -U zeroth -d zeroth' --health-interval 1s \
  --health-timeout 5s --health-retries 60 \
  -e POSTGRES_USER=zeroth -e POSTGRES_PASSWORD=zeroth -e POSTGRES_DB=zeroth "$POSTGRES"
docker run -d --rm --platform linux/arm64 --name load-gate-redis --network load-gate \
  --health-cmd 'redis-cli ping' --health-interval 1s --health-timeout 5s --health-retries 60 \
  "$REDIS"
for service in load-gate-postgres load-gate-redis; do
  for attempt in $(seq 1 60); do
    health_state=$(docker inspect --format '{{.State.Health.Status}}' "$service")
    [ "$health_state" = healthy ] && break
    [ "$attempt" -eq 60 ] && exit 1
    sleep 1
  done
done
uv build
WHEEL=$(find dist -maxdepth 1 -name '*.whl' -print -quit)
SDIST=$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)
uv run python release/gates/cli.py identity \
  --artifact "zeroth-core-wheel=$WHEEL" \
  --artifact "zeroth-core-sdist=$SDIST" \
  --compatibility release/langgraph/compatibility.json \
  --output release/evidence/candidate-identity.json
docker run --rm --platform linux/arm64 --network load-gate --cpus 2 --memory 8g \
  -v "$PWD:/work" -w /work \
  -e ZEROTH_LOAD_POSTGRES_DSN=postgresql://zeroth:zeroth@load-gate-postgres:5432/zeroth \
  -e ZEROTH_LOAD_REDIS_URL=redis://load-gate-redis:6379/14 \
  -e ZEROTH_TEST_REDIS_URL=redis://load-gate-redis:6379/15 \
  -e ZEROTH_LOAD_OBSERVATIONS=release/evidence/load-recovery-raw.json \
  -e ZEROTH_LOAD_RUNTIME_IMAGE="$RUNTIME" \
  -e ZEROTH_LOAD_POSTGRES_VERSION="$POSTGRES" \
  -e ZEROTH_LOAD_REDIS_VERSION="$REDIS" \
  "$RUNTIME" sh -c 'python -m pip install uv==0.11.6 && \
  uv sync --frozen --all-groups --all-extras && uv run pytest -q \
  tests/load_release/test_product_profiles.py::test_real_product_fairness_fault_and_overload_evidence'
```

Bind the raw observations to the measured candidate and evaluate them:

```bash
uv run python release/load/harness.py run \
  --profiles release/load/profiles-v1.json \
  --baseline release/load/baseline-v1.json \
  --identity release/evidence/candidate-identity.json \
  --observations release/evidence/load-recovery-raw.json \
  --output release/evidence/load-recovery-benchmark.json
docker rm -f load-gate-postgres load-gate-redis
docker network rm load-gate
```

The report retains the candidate identity and every raw per-request timestamp,
lifecycle/run ID, tenant, deployment, replica, worker, surface, fault, status,
`Retry-After`, latency, queue, CPU and memory value. This is sufficient to
independently recompute throughput, p50/p95/p99, fairness, recovery time and
lost/duplicate accepted IDs instead of trusting the report summaries.

CI uploads `release/evidence/load-recovery*`: the raw rows, benchmark report,
JUnit output and gate record. They are retained for at least 30 days even when
the job fails. A missing, malformed, threshold-regressed, or differently-bound
file blocks the candidate verdict.

## Reading a blocked verdict

The verdict names one status per gate. The five refusal reasons are distinct
because they need different responses:

| Status | Meaning | What to do |
|---|---|---|
| `missing` | No record was produced | Find the job that failed before it could emit one |
| `stale` | The record describes an earlier commit | Re-run the gate against the candidate |
| `partial` | The record does not cover every required result or evidence file | The gate ran incompletely; check for a skipped step |
| `mismatched` | The record is bound to a different build at the same commit | Evidence came from another build; re-run against the published artifacts |
| `failed` | The gate ran and did not pass | Fix the underlying failure |

An empty gate set is never releasable: "no gates ran" is not "all gates
passed".

## Running the gates locally

Measure the candidate:

```bash
python release/gates/cli.py identity --output release/evidence/candidate-identity.json
```

Validate whatever evidence exists, which fails closed when records are absent:

```bash
python release/gates/cli.py validate --identity release/evidence/candidate-identity.json --phase candidate
```

Render the human-readable verdict:

```bash
python release/gates/cli.py verdict --identity release/evidence/candidate-identity.json --phase candidate
```

Run the fast security subset with the same portable launcher as pull requests:

```bash
uv run python -m release.security.pytest_gate --matrix release/security/security-matrix.json --tier pr-critical --results release/evidence/security-pr-outcomes.json --junitxml release/evidence/security-pr-junit.xml --pytest-arg=-q
```

## Adding a gate

Add it to `release/gates/release-gates.json` with the results it requires, the
evidence kinds it produces and the identity facets it binds, then emit its
record from CI with `cli.py record`. Promotion depends on the validator, not on
a job list, so a new gate tightens promotion without any change to the workflow
dependency graph. `tests/release_gates` will require the new gate to have
exactly one producing job and to be able to block promotion on its own.
