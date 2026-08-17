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
  --zeroth-version 0.23.9.12 \
  --zeroth-ref <FULL_ZEROTH_COMMIT_SHA>
```

The command refuses to overwrite existing files and emits:

- `.github/workflows/app-certification.yml`;
- `certification.json`;
- `Dockerfile.certification`;
- `<module>/certification_healthcheck.py`;
- `<module>/migrations.py`.

The generated module must expose `graphs.build_graph`, `contracts.CONTRACTS`,
`entrypoint.build_auth_config`, `entrypoint.build_policy_guard`, and
`migrations.migrate`. Adjust only the structured `targets` references if the
app uses different names. The declaration cannot provide commands or shell
text.

## What is checked

Certifier-owned implementations load the structured targets through the app's
synced `.venv` and apply the same semantic boundaries used for publication:

- register every Pydantic contract in a migrated temporary database;
- validate every graph with the complete runtime validator, including custom
  reducer imports, and resolve every referenced contract;
- resolve policy and capability bindings and require allowed decisions;
- validate service authentication, the frozen lock, installed extras,
  the declared app migration against a fresh database, container state,
  readiness JSON, and frontend/API drift;
- send the same deterministic smoke request to packaged and tmpfs-backed
  candidate containers.

Each host-side check runs in a bounded subprocess without an ambient shell.
Candidate imports run in a low-privilege child whose stdout is diagnostic only;
a separate supervisor validates semantic evidence after that child exits, and
the trusted runner validates it again before recording a pass. Docker state and
the locked frontend tool tree are checked on the trusted path. Container
readiness requires a parsed JSON body with `status: ok`; HTTP 200 by itself is
not sufficient. Smoke requests refuse redirects, and frontend targets must
remain below the app checkout after symlink resolution.

## Identity, evidence, and privileges

The `certify` job has only `contents: read`. App dependency hooks and semantic
imports run as a dedicated local user that can write only its isolated virtual
environment and frontend copy. The locked frontend dependencies are copied to
a runner-owned read-only tool tree; the pinned certifier and handoff remain
runner-owned. The job builds from an exact Git archive and measures its SHA-256
alongside the app commit and local image descriptor, generates an SPDX SBOM, and
writes a canonical report even when preparation, build, startup, or health
fails. Jobs, candidate process trees, containers, archives, HTTP exchanges, and
retained logs all have explicit bounds.

The declared Dockerfile must resolve to a regular file inside that exact Git
archive. The measured image must also retain the isolated absolute-interpreter
runtime command emitted by the scaffold; both candidate modes import Zeroth from
the verified wheel location before starting the application module.

A fresh, unprivileged `verify` job downloads that handoff and uses a clean
pinned Zeroth checkout to validate its source archive, hashes, Docker/OCI
descriptor tree, config, and layers. It emits a digest-bound verdict that
candidate code cannot rewrite. Only after verification succeeds does the
separate `attest` job
receive `id-token: write`, `attestations: write`, and
`artifact-metadata: write`; it authenticates the verdict before signing the
exact image subject. Candidate code is never checked out or executed in either
post-certification job.

The final report cross-binds the app commit, source archive digest, exact Zeroth
version and commit, image name and digest, SPDX package inventory and subject,
certifier wheel, a byte-for-byte inventory of that wheel as installed in the
image, locked image requirements, signed provenance predicate, and hashes of
all retained evidence. The installation inventory is measured from a stopped
container without executing candidate code. The finalizer cryptographically
verifies the bundle against the expected GitHub OIDC issuer, repositories,
workflow, and commits before replacing the unsigned predicate. A hand-written
or tampered passing report is rejected.

## Retained diagnostics

The workflow retains the canonical JSON report, stage outcomes, declaration,
source and image archives, SPDX JSON, signed provenance bundle, container
inspection, and container logs for 14 days. The in-repository `vendor-dd`
reference can be run from **Actions → Certify vendor-dd → Run workflow**.
When certification runs and fails, its validated per-check diagnostics are
retained instead of being replaced by generic workflow-stage failures.
