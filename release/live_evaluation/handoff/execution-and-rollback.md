# Live evaluation execution and rollback

Campaign: `evaluation-studio-v1`

These instructions operate the persistent local development topology without
reinitializing campaign state. Run them from the Zeroth worktree. Secrets remain
in the external secret provider; do not place provider or signing keys in shell
history, repository files, screenshots, or evidence command output.

## Execution preflight

Before starting any service, verify the security-mode configuration:

- production has `ZEROTH_AUTH__BROWSER_SESSION_SECRET` supplied by the external
  secret provider (at least 32 bytes and shared by every worker);
- `ZEROTH_SANDBOX__ALLOW_UNTRUSTED_LOCAL_DEVELOPMENT` and
  `ZEROTH_SANDBOX__ALLOW_UNISOLATED_MCP_DEVELOPMENT` are unset/false;
- MCP, when enabled, has a scanned image pinned by digest, `network=none` unless
  required, and external firewall policy for any permitted egress;
- untrusted repository/inline execution selects Docker or sidecar;
- budget `fail_closed` remains true; and
- gateway-only deployments do not set `require_full_tool_enforcement`; use the
  in-process SDK enforcement path when that guarantee is required.

If the console is standalone, configure one exact
`ZEROTH_CONSOLE_CORS_ORIGINS` origin, HTTPS, credentialed requests, and a static-
host document CSP whose `connect-src` names only the Zeroth API. Mounted mode
uses same-origin CSP and needs no CORS.

1. Confirm the persistent services and exact served graph:

   ```sh
   docker compose -f compose.dev.yml ps
   curl -fsS http://127.0.0.1:8122/health
   curl -fsS http://127.0.0.1:8123/health
   curl -fsS http://127.0.0.1:8121/api/v2/heartbeat
   ```

2. Start or reconcile the persistent topology without deleting volumes:

   ```sh
   docker compose -f compose.dev.yml up -d chroma backend backend-twin frontend
   ```

3. Open the current UI at `http://127.0.0.1:3000/`. The primary API is
   `http://127.0.0.1:8122`; the twin tenant API is `http://127.0.0.1:8123`.

4. Verify the provider-independent regression baseline before a campaign slice:

   ```sh
   uv run pytest -q tests/live_evaluation
   npm --prefix frontend test -- --run
   npm --prefix frontend run build
   ```

5. Before paid calls, verify the campaign cap is `$10`, the per-run cap is
   `$0.25`, audit readiness is `signed`, the credential is newly rotated, and
   the operation/run/campaign tags are present. Fail closed on any unknown.

### Rotated provider credential

Install a newly rotated credential through hidden terminal input into the
external campaign state. The installer is create-only and refuses repository
destinations; it never prints the value.

```sh
uv run python scripts/install_zeroth_provider_credential.py \
  --repository-root "$PWD" \
  --destination "$HOME/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets/provider.env"
```

Then recreate both tenant backends with the same external reference while
pinning the frozen primary serving target:

```sh
ZEROTH_DEV_ENV_FILE="$HOME/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets/provider.env" \
ZEROTH_DEV_DEPLOYMENT_REF="provider-free-child-approval-d012-20260826-2-parent" \
docker compose -f compose.dev.yml up -d --force-recreate backend backend-twin
```

Do not reuse `market_analysis/.env`, `.dev-secrets/zeroth.env`, a credential
previously pasted into chat, or a provider window synthesized as zero. Verify
both health endpoints and the authoritative provider-usage baseline before
arming any paid command.

## Bounded service restart

Restart only the service whose code changed; persistent databases, Chroma, the
action sink, and append-only evidence stay outside the container lifecycle.

```sh
docker compose -f compose.dev.yml up -d --force-recreate frontend
docker compose -f compose.dev.yml up -d --force-recreate backend
```

After a backend restart, `/health` must report the intended deployment and exact
graph version before any run is submitted.

## Deployment rollback and roll-forward

The backend serving target is selected by `ZEROTH_DEV_DEPLOYMENT_REF`. Set the
override in the external development environment to a previously registered
deployment reference, then recreate only `backend`. Do not delete deployment,
run, approval, audit, cost, or action-receipt history.

```sh
ZEROTH_DEV_ENV_FILE="$HOME/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets/provider.env" \
docker compose -f compose.dev.yml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8122/health
```

Acceptance requires the health response to show the rollback deployment's exact
graph version. Roll forward by restoring the intended deployment reference and
repeating the same bounded recreate and health check. Registration is not
serving; a restart is required after selecting a version.

## Code and UI rollback

- Revert the specific reviewed commit that introduced the faulty slice; do not
  use `git reset --hard` or discard unrelated working-tree changes.
- Rebuild/recreate only the affected service.
- For schema changes, stop writers and restore the named pre-migration database
  snapshot into a new recovery file. Never run an ad-hoc destructive downgrade
  against the only campaign database.
- Preserve action-sink and audit databases so ambiguous outcomes and signed
  operator resolutions remain authoritative.
- Preserve all evidence roots. A bad or partial attempt is superseded by a new
  append-only root; it is never edited or deleted.
- Rolling back browser-session support reintroduces browser-readable persistent
  credentials and is not an acceptable security rollback. Roll forward or stop
  the console instead. If signing material is rotated, expect all current
  sessions to expire and require a fresh exchange.
- Do not recover an MCP outage by enabling the unisolated development flag in a
  pilot/production environment. Keep registered tool nodes intact and fix or
  roll back the pinned isolation image/profile; unset the image to fail closed.
- If repository ingress must be disabled during recovery, remove/disable GitHub
  App configuration and stop its worker after preserving checkout/run/audit
  records. Do not delete installation claims or staged evidence as a shortcut.

## Verification and evidence recovery

Verify any sealed root from inside that root:

```sh
shasum -a 256 -c SHA256SUMS
```

If a run times out after a possible side effect, do not resubmit it. Keep the
operation `AMBIGUOUS`, perform the one authoritative outcome lookup, and use the
authorized operator-resolution API only with a reason and optional receipt.
The signed resolution must remain linked to the run, approval, action operation,
audit chain, and economics record.

## Stop and recovery rules

Stop immediately on a secret-shaped artifact, cross-tenant data, broken audit
chain, unexplained economics difference, cap violation, unintended external
action, or visual-regression failure. Record the discrepancy, retain ambiguous
reservations, reconcile the affected subsystem, and resume in a new evidence
root only after the stop condition has direct proof of resolution.
