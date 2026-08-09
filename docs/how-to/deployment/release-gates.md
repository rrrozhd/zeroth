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
| 5 | Deployment smoke | The image starts, reports ready, serves the gateway, refuses invalid configuration, drains on shutdown, and carries an SBOM and a verified provenance attestation | release candidate |
| 6 | Remote acceptance | The candidate installs from the published index and the published artifact runs end to end | release candidate |
| 7 | Promotion | Every preceding gate validated, and a named human accepted the release | release candidate, manual |

Gates 1–5 are the **candidate** phase and gate TestPyPI. Gates 6–7 are the
**final** phase and gate PyPI.

## Responsibilities by trigger

### Pull request

Pull requests keep the fast checks only: `ci.yml`, `docs.yml`, `examples.yml`,
`langgraph-compatibility.yml` and `verify-extras.yml`. The gate matrix has no
`pull_request` trigger, so opening a PR never pays for a Docker build, an SBOM,
an attestation or a TestPyPI round trip. A test asserts this rather than
trusting the convention.

### Nightly

`release-gates.yml` runs on a schedule and produces gates 1–4, so drift is
found before a release is cut rather than during one. It validates exactly the
gates it produces — a nightly is never blocked by evidence only a release
candidate can generate.

Run the same set on demand from the Actions tab (**Release gates** →
**Run workflow**).

### Release candidate

Publishing a GitHub Release runs `release-zeroth-core.yml`. It builds once,
calls the nightly workflow to gather gates 1–4 against **that** build, adds
gates 5–7, and validates:

- `evidence-gate` validates the candidate phase; TestPyPI publication depends
  on it.
- `evidence-gate-final` validates every gate including the promotion signoff;
  PyPI publication depends on it.

Both verdicts are written to the job summary and retained as artifact bundles
for 90 days.

### Manual

One piece of evidence has no CI producer: the promotion signoff. Before
promoting to PyPI, a named human records acceptance at
`release/signoff/<version>.md` — for example `release/signoff/0.19.md` — and
commits it on the release tag. Absent that file, the promotion gate reports
failure and PyPI stays closed. The file should say who accepted the release and
what they checked.

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

## Adding a gate

Add it to `release/gates/release-gates.json` with the results it requires, the
evidence kinds it produces and the identity facets it binds, then emit its
record from CI with `cli.py record`. Promotion depends on the validator, not on
a job list, so a new gate tightens promotion without any change to the workflow
dependency graph. `tests/release_gates` will require the new gate to have
exactly one producing job and to be able to block promotion on its own.
