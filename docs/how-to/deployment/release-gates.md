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
| 6 | Deployment smoke | The image starts, reports ready, serves the gateway, refuses invalid configuration, drains on shutdown, and carries an SBOM and a verified provenance attestation | release candidate |
| 7 | Remote acceptance | The candidate installs from the published index and the published artifact runs end to end | release candidate |
| 8 | Promotion | Every preceding gate validated, and a named human accepted the release | release candidate, manual |

Gates 1–6 are the **candidate** phase and gate TestPyPI. Gates 7–8 are the
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

`release-gates.yml` runs on a schedule and produces gates 1–5, so drift is
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
calls the nightly workflow to gather gates 1–5 against **that** build, adds
gates 6–8, and validates:

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
