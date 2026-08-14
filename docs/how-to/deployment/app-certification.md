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
  --zeroth-version 0.23.9.1 \
  --zeroth-ref <FULL_ZEROTH_COMMIT_SHA>
```

The command refuses to overwrite existing files and emits:

- `.github/workflows/app-certification.yml`;
- `certification.json`;
- `Dockerfile.certification`;
- `<module>/certification_healthcheck.py`.

The generated module must expose `graphs.build_graph`, `contracts.CONTRACTS`,
`entrypoint.build_auth_config`, `entrypoint.build_policy_guard`, and
`entrypoint.main`. Adjust only the structured `targets` references if the app
uses different names. The declaration cannot provide commands or shell text.

## What is checked

Certifier-owned implementations load the structured targets through the app's
synced `.venv` and apply the same semantic boundaries used for publication:

- register every Pydantic contract in a migrated temporary database;
- validate every graph and resolve every referenced contract;
- resolve policy and capability bindings and require allowed decisions;
- validate service authentication, the frozen lock, installed extras,
  migrations, container state, readiness JSON, and frontend/API drift;
- send the same deterministic smoke request to packaged and tmpfs-backed
  candidate containers.

Each host-side check runs in a bounded subprocess without an ambient shell.
Container readiness requires a parsed JSON body with `status: ok`; HTTP 200 by
itself is not sufficient.

## Identity, evidence, and privileges

The unprivileged `certify` job runs all app and container code with only
`contents: read`. It measures the exact app commit and local image digest,
generates an SPDX SBOM, and writes a canonical report even when preparation,
build, startup, or health fails.

Only after that job succeeds does the separate `attest` job receive
`id-token: write`, `attestations: write`, and `artifact-metadata: write`. It
downloads the immutable handoff, executes only the pinned Zeroth verifier,
recomputes the report and evidence hashes, proves the Docker archive config
digest, and then signs the exact image subject. Candidate code is never checked
out or executed in the privileged job.

The final report cross-binds the app commit, exact Zeroth version, image name
and digest, SPDX subject, signed provenance predicate, and hashes of both
retained evidence files. A hand-written or tampered passing report is rejected.

## Retained diagnostics

The workflow retains the canonical JSON report, stage outcomes, declaration,
image archive, SPDX JSON, signed provenance bundle, container inspection, and
container logs for 14 days. The in-repository `vendor-dd` reference can be run
from **Actions → Certify vendor-dd → Run workflow**.
