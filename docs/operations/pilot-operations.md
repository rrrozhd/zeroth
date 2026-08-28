# Pilot operations

## Operating boundary

The local pilot runs the console, primary and twin-tenant services, Chroma, and
Redis through `compose.dev.yml`. Durable state lives outside the repository
under `~/.local/share/zeroth/evaluations/`; provider and service credentials
live in the ignored `.dev-secrets/zeroth.env` and generated `runtime-secrets`
directories. Never copy either directory into an evidence bundle.

This topology is suitable for a supervised pilot and recovery rehearsal. It is
not a distributed high-availability production design.

## Start, inspect, and stop

```bash
docker compose -f compose.dev.yml up -d --build
docker compose -f compose.dev.yml ps
curl --fail --silent http://127.0.0.1:8122/health | jq .
curl --fail --silent http://127.0.0.1:8123/health | jq .
docker compose -f compose.dev.yml stop
```

The console is at `http://127.0.0.1:3000/console/`. Confirm the authenticated
tenant, role, environment, and served deployment in its top bar before making
any change.

## Bind the server-owned artifact identity

Build the backend from a clean checkout, measure Docker's immutable image ID,
then write the commit/image pair to the private environment file:

```bash
docker compose -f compose.dev.yml build backend
uv run python scripts/configure_dev_artifact_identity.py \
  --image zeroth-dev-backend:latest \
  --env-file .dev-secrets/zeroth.env
docker compose -f compose.dev.yml up -d --force-recreate backend backend-twin
```

Both tenant services intentionally consume the same measured backend image.
Do not give `backend-twin` an independent build target while sharing one
identity setting; that would allow the two processes to report a digest that is
true for only one of them.

The configurator refuses a dirty tracked checkout, malformed Git commit, or
non-`sha256` Docker identity. It preserves unrelated private settings and
updates the pair atomically with mode `0600`. The API never accepts these
values from a client. Identity alone does not imply certification: production
readiness also requires a trusted promotion receipt bound to the exact served
deployment.

## Backup and restore drill

1. Record the served deployment, schema revision, database hashes, and an
   authenticated run that must survive.
2. Stop only the disposable service whose state will be copied.
3. Copy its complete state directory to the external `state-snapshots` root;
   never place databases or credentials under the public evidence root.
4. Start the service and verify health plus the selected run, audit chain,
   economics event, artifact, and legal hold.
5. Simulate an outage by stopping the service and confirming the endpoint is
   unavailable, then restart and repeat the checks.
6. Stop it again, move the live state aside recoverably, restore the snapshot,
   restart, and require the restored database hash and selected records to
   match.

The 2026-08-28 disposable drill followed this procedure and restored the exact
database hash. It is technical evidence only; the named operations owner must
repeat or witness the drill and sign the record below.

## Incident response

- Stop admission when tenant scope, audit integrity, economics reconciliation,
  or artifact identity is uncertain.
- Preserve logs and state before remediation. Do not retry an ambiguous
  side-effecting operation; use authoritative outcome lookup and the signed
  operator-resolution path.
- Restore from the last verified external snapshot, then check schema, health,
  served deployment, signed audit continuity, open reservations, and holds.
- Record detection time, containment, recovery time, affected scope, evidence
  root, and follow-up action in `release/live_evaluation/PILOT_SIGNOFF.md`.

## Operations-owner acceptance

The owner cannot be assigned by code. A real accountable person must complete
these fields before pilot acceptance:

- name and reachable escalation channel;
- coverage window and backup delegate;
- recovery objective and stop authority;
- drill evidence root and witnessed timestamp; and
- dated signature acknowledging credential, retention, audit, economics, and
  ambiguous-operation responsibilities.

Until that record is signed, the technical drill is complete but the
operations-ownership gate remains blocked.
