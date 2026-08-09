# Run deployed acceptance

The deployed acceptance harness tests a selected Zeroth deployment from outside its process. A passing report is evidence about the candidate identity file, image digest, deployment URL, deployment reference, and one dedicated test tenant.

## Target contract

Use a tenant whose ID begins with `acceptance-`. The harness rejects `default`, production-looking tenant IDs, URL credentials, non-HTTP URLs, missing role credentials, and candidate identities without an immutable image digest before it makes a network request.

The target serves the native Zeroth APIs and nothing test-only: `release/acceptance/contracts/zeroth-v1.json` targets the same `/health`, `/v1/runs`, `/v1/deployments`, `/v1/retention` and studio routes any client uses. Restart and drain are not routes — they are lifecycle operations a `LifecycleController` performs on the deployment, so the product never ships an endpoint that restarts itself. Missing endpoints or unsupported Agent Server versions fail the suite; they are never recorded as skips.

## The two legs

The suite runs against two targets, and they are authoritative for different scenarios.

| Leg | Target | Proves | Runs |
|---|---|---|---|
| Ephemeral | the real service booted by `tests/acceptance/ephemeral.py` against a file-backed database | readiness, authentication, RBAC, migrations, workflow lifecycle, deployment, runs, approvals, audit, artifacts, retention, executable-unit failures, restart recovery, shutdown — fourteen of the seventeen | the default test suite, on every change |
| Remote | a deployed candidate with a live Agent Server behind the gateway | all seventeen, adding gateway HTTP, gateway WebSocket, and Agent Server compatibility | the release workflow, bound to the candidate image |

Neither leg skips a scenario. `AcceptanceRunner` records a result for all seventeen every time, and the `remote-acceptance` release gate requires every one of them to pass in the report it consumes. The ephemeral test pins its own partition exactly, so a scenario that quietly stops passing there fails the build rather than disappearing.

The operator, reviewer, and admin credentials must be distinct and must all resolve to the configured tenant. Put credential values in environment variables only:

```bash
export ZEROTH_ACCEPTANCE_OPERATOR_KEY='...'
export ZEROTH_ACCEPTANCE_REVIEWER_KEY='...'
export ZEROTH_ACCEPTANCE_ADMIN_KEY='...'
```

Create a configuration file containing environment variable names, not secrets:

```json
{
  "schema_version": 1,
  "base_url": "https://acceptance.example.net",
  "tenant_id": "acceptance-release",
  "deployment_ref": "candidate",
  "candidate_identity": "release/evidence/candidate-identity-full.json",
  "credentials": {
    "operator": "ZEROTH_ACCEPTANCE_OPERATOR_KEY",
    "reviewer": "ZEROTH_ACCEPTANCE_REVIEWER_KEY",
    "admin": "ZEROTH_ACCEPTANCE_ADMIN_KEY"
  },
  "lifecycle": {
    "restart_url": "/__acceptance/restart",
    "shutdown_url": "/__acceptance/shutdown"
  }
}
```

Run the harness:

```bash
python -m release.acceptance.cli \
  --config acceptance-config.json \
  --contract release/acceptance/contracts/zeroth-v1.json \
  --output release/evidence/deployed-acceptance-report.json
```

The process exits 0 only when all 17 required scenarios and every cleanup operation pass. Reports never contain credential values. Redirects are refused, response sizes and deadlines are bounded, and DELETE operations are limited to the invocation namespace or resource IDs captured from resources created by that invocation.

## CI entry points

The release workflow runs the harness against the repository-configured URL and tenant, then records the report as the `deployment` evidence for `remote-acceptance`. That gate now binds commit, package, and image identity; promotion fails if the suite is missing, failed, or belongs to another image.

For an operator-selected deployment, run the **Deployed acceptance** workflow with the deployment URL, tenant, deployment reference, and release run ID containing `candidate-identity-full`. The workflow downloads that identity, runs the same contract, retains the report, and fails visibly on incomplete capability or cleanup evidence.

## Scope boundaries

This harness calls the LangGraph governance surfaces built under ZER-1; it does not duplicate their policy implementation. It performs boundary authentication, RBAC, and tenant-safety checks, while ZER-32 remains authoritative for the exhaustive hostile and cross-tenant security matrix. Repository checkout and hardened staging are owned by ZER-37. Zeroth has no project-artifact concept today, so `executable_unit_failures` asserts what the product does emit: a run whose input cannot be resolved against the deployment's contract is rejected outright rather than accepted and silently dropped.
