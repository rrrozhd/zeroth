# Certify a generated application

Use the certification scaffold to add the declaration, reusable-workflow
caller, fail-closed readiness probe, and certification Dockerfile together.
The workflow builds and measures a candidate but does not push or deploy it.

## Generate the assets

Run the pinned Zeroth checkout's scaffold command from the generated app root:

```bash
PYTHONPATH=/path/to/zeroth python -m release.app_certification scaffold \
  --root . \
  --app-name my-app \
  --module my_app \
  --zeroth-version 0.23.9.19 \
  --zeroth-ref <FULL_ZEROTH_COMMIT_SHA>
```

The command refuses to overwrite existing files and emits:

- `.github/workflows/app-certification.yml`;
- `certification.json`;
- `certification.semantic.json`;
- `Dockerfile.certification`;
- `<module>/certification_entrypoint.py`;
- `<module>/certification_healthcheck.py`;
- `<module>/migrations.py`.

The generated module must already expose the graph, contract, authentication,
and policy factories named by the standard targets. The scaffold executes those
generator-owned inputs once and emits their complete deterministic semantic
manifest, including target-source hashes. Refresh it after changing a target:

```bash
PYTHONPATH=/path/to/zeroth python -m release.app_certification generate-semantic \
  --root . --declaration certification.json \
  --output certification.semantic.json --database-backend sqlite
```

The generation command normalizes volatile graph timestamps and writes the
canonical JSON atomically, so identical inputs produce identical bytes. Adjust
only the structured `targets` references if the app uses different names.
Neither declaration can provide commands or shell text.

The reusable workflow binds the validated semantic backend to the certification
process and both runtime containers. It provisions isolated SQLite storage; a
manifest declaring PostgreSQL fails closed because that workflow does not own a
fresh PostgreSQL database or DSN. Direct certification of PostgreSQL requires an
explicit matching backend and fresh DSN.

## What is checked

Certifier-owned implementations validate the source-bound static semantic
document without importing candidate Python:

- register every Pydantic contract in a migrated temporary database;
- validate every graph with the complete runtime validator and resolve every
  referenced contract;
- resolve policy and capability bindings and require allowed decisions;
- validate service authentication, the frozen lock and declared Zeroth version,
  the declared app migration against a fresh database, container state,
  readiness JSON, and frontend/API drift;
- import the exact installed runtime distribution and exercise authenticated
  Regulus capability, budget, and instrumentation operations in both modes;
- send the same deterministic smoke request to packaged and tmpfs-backed
  candidate containers.

Arbitrary Python cannot prove that a returned object represents its real
behavior, so dynamic graph, contract, service, policy, and optional-import
results are outside this certification contract. App-local callable reducers
are likewise unsupported; use statically valid graph behavior or a separately
reviewed integration. The only candidate Python executed for a verdict is the
declared migration, and its output is ignored: the trusted supervisor accepts
only the independently inspected fresh-database effect.

Each host-side check runs in a bounded subprocess without an ambient shell.
The migration runs as a fresh locked low-privilege user with a private primary
group. On Linux the supervisor becomes a child subreaper and terminates adopted
run descendants through stable pidfds. The final UID inventory is only a
fail-closed leak signal and never authorizes an account-wide kill. Docker state
and the locked frontend tool tree are checked on the trusted path. Container
readiness requires a parsed JSON body with `status: ok`; HTTP 200 by itself is
not sufficient. Smoke requests refuse redirects, and frontend targets must
remain below the app checkout after symlink resolution.

The public `run` command requires Linux pidfd containment and
`--untrusted-user` to name an existing, non-root account distinct from the
certifier. It must have a locked password, a `nologin` or `false` login shell,
a same-name private primary group, no supplementary groups, no sudo rules, and
no pre-existing processes. Candidate execution also sets `no_new_privs` and
drops inherited, ambient, and bounding capabilities. The declaration,
candidate root, report directory, and
`--evidence-root`, including both resolved and lexical ancestor chains, must
not be writable or replaceable by that account under the kernel's effective
access checks, including named POSIX ACLs. Omitted, shared, active, privileged,
or writable-result configurations fail before candidate execution.

## Identity, evidence, and privileges

The `certify` job has only `contents: read`. The candidate Dockerfile runs in a
named disposable BuildKit scope with bounded CPU, memory, processes, output,
and fixed-size state storage. App dependency hooks run inside a digest-pinned,
read-only container with the same resource classes and a fixed-size
virtual-environment filesystem. Docker daemon logs use a rotating size cap,
and the complete named build and dependency scopes are removed and inventoried
after every outcome. Every Docker name and image tag includes the immutable
workflow run/attempt identity; collisions fail before creation, and image cleanup
requires the exact recorded image IDs. The pinned certifier and handoff remain runner-owned. The job builds
from an exact Git archive and measures its SHA-256
alongside the app commit and local image descriptor, generates an SPDX SBOM, and
writes a canonical report even when preparation, build, startup, or health
fails. Jobs, candidate process trees, containers, archives, HTTP exchanges, and
retained logs all have explicit bounds.

The declared Dockerfile must resolve to a regular file inside that exact Git
archive. The measured image must also retain the isolated absolute-interpreter
runtime command emitted by the scaffold; both candidate modes import Zeroth from
the verified wheel location before starting the application module.

A fresh, unprivileged `verify` job downloads that handoff and uses clean,
exact app and pinned Zeroth checkouts to validate both checkout HEADs and the
source archive against the
externally resolved commit tree, plus its hashes and Docker/OCI
descriptor tree, config, and layers. It emits a digest-bound verdict that
candidate code cannot rewrite. Only after verification succeeds does the
separate `attest` job
receive `id-token: write`, `attestations: write`, and
`artifact-metadata: write`; it authenticates the verdict before signing the
exact image subject. The app tree is checked out only as immutable comparison
material and is never executed in either post-certification job.

The final report and verifier verdict cross-bind the app commit and tree,
source archive digest, exact Zeroth
version and commit, image name and digest, SPDX package inventory and subject,
certifier wheel, a byte-for-byte inventory of that wheel as installed in the
image, locked image requirements, signed provenance predicate, and hashes of
all retained evidence, including cleanup inventory and exact workflow-stage
outcomes. The installation inventory is measured from a stopped
container without executing candidate code. The finalizer cryptographically
verifies the bundle against the expected GitHub OIDC issuer, repositories,
workflow, and commits before replacing the unsigned predicate. A hand-written
or tampered passing report is rejected.

## Retained diagnostics

The workflow retains the canonical JSON report, hashed stage outcomes, hashed
post-cleanup absence inventory, declaration,
source and image archives, SPDX JSON, signed provenance bundle, container
inspection, and container logs for 14 days. The in-repository `vendor-dd`
reference can be run from **Actions → Certify vendor-dd → Run workflow**.
When certification runs and fails, its validated per-check diagnostics are
retained instead of being replaced by generic workflow-stage failures.
