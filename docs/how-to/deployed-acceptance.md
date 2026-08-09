# Run deployed acceptance

The deployed acceptance harness tests a selected Zeroth deployment from outside its process. A passing report is evidence about the candidate identity file, image digest, deployment URL, deployment reference, and one dedicated test tenant.

## Target contract

Use a tenant whose ID begins with `acceptance-`. The harness rejects `default`, production-looking tenant IDs, URL credentials, non-HTTP URLs, missing role credentials, and candidate identities without an immutable image digest before it makes a network request.

The target must provide the native Zeroth APIs used by the suite and the test-only control endpoints declared in `release/acceptance/contracts/zeroth-v1.json`. Those control endpoints expose deterministic fixture state and lifecycle authority for migrations, runs, streams, approval counts, audit, artifacts, executable-unit failures, restart, drain, and shutdown. They belong on a dedicated acceptance deployment, not a production deployment. Missing endpoints or unsupported Agent Server versions fail the suite; they are never recorded as skips.

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

The process exits 0 only when all 18 required scenarios and every cleanup operation pass. Reports never contain credential values. Redirects are refused, response sizes and deadlines are bounded, and DELETE operations are limited to the invocation namespace or resource IDs captured from resources created by that invocation.

## CI entry points

The release workflow runs the harness against the repository-configured URL and tenant, then records the report as the `deployment` evidence for `remote-acceptance`. That gate now binds commit, package, and image identity; promotion fails if the suite is missing, failed, or belongs to another image.

For an operator-selected deployment, run the **Deployed acceptance** workflow with the deployment URL, tenant, deployment reference, and release run ID containing `candidate-identity-full`. The workflow downloads that identity, runs the same contract, retains the report, and fails visibly on incomplete capability or cleanup evidence.

## Scope boundaries

This harness calls the LangGraph governance surfaces built under ZER-1; it does not duplicate their policy implementation. It performs boundary authentication, RBAC, and tenant-safety checks, while ZER-32 remains authoritative for the exhaustive hostile and cross-tenant security matrix. Repository checkout and hardened staging are owned by ZER-37; until those surfaces exist, this suite requires explicit unresolved and unstaged artifact failures.
