# Certify a generated application

Use the reusable workflow to prove that one app commit and one locally built
image pass every declared boundary. The workflow does not push an image,
publish to a registry, or deploy the candidate.

## Call the workflow

Pin both the reusable workflow and its certifier checkout to the same full
Zeroth commit SHA. Branches and tags are rejected for `zeroth_ref`.

```yaml
permissions:
  contents: read
  attestations: write
  id-token: write

jobs:
  certify:
    uses: rrrozhd/zeroth/.github/workflows/app-certification.yml@<FULL_ZEROTH_COMMIT_SHA>
    with:
      zeroth_ref: <FULL_ZEROTH_COMMIT_SHA>
      declaration_path: path/to/certification.json
```

The reusable workflow also declares the same three least-privilege permissions;
GitHub requires them on both sides of a reusable attestation workflow.

## What the workflow measures

The app commit comes from `git rev-parse HEAD` in the app checkout. The image
digest comes from `docker image inspect` after the declared Dockerfile is built.
The SBOM is evidence about that image, never the source of either identity.

Two containers start from the exact measured image:

- a packaged boundary with its own Docker volume and network;
- an ephemeral boundary with a tmpfs data directory and separate network.

Both must report healthy before the same deterministic smoke request is sent to
their distinct URLs. Any failed argv check, identity mismatch, unhealthy
container, HTTP mismatch, missing lock, SBOM, or provenance bundle makes the
report fail.

## Declaration rules

Start from
[`apps/vendor_dd/certification.json`](https://github.com/rrrozhd/zeroth/blob/main/apps/vendor_dd/certification.json).
A declaration must provide exactly the 14 mandatory checks as non-empty,
unique argv arrays. Commands run directly without an ambient shell. It must
also pin an exact numeric Zeroth version and name safe relative paths for its
lock, Dockerfile, SBOM, and provenance bundle.

Authenticated smoke requests map HTTP header names to environment variable
names:

```json
"headers_from_env": {
  "X-API-Key": "APP_CERTIFICATION_API_KEY"
}
```

Never put a token in the declaration. The workflow generates and masks
`APP_CERTIFICATION_API_KEY`, passes it to both containers, and resolves it only
when building the HTTP request. A missing or empty mapped variable fails before
any smoke request is attempted.

## Retained diagnostics

The workflow always attempts to upload the declaration, certification report,
SPDX JSON, provenance bundle, container inspection, and logs. It then stops and
removes both containers, their isolated networks, and the packaged data volume,
including after a failed certification step.

The in-repository `vendor-dd` caller is manual:

```text
Actions -> Certify vendor-dd -> Run workflow
```
