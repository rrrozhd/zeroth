# 3. Run in service mode with an approval gate

Section 2 drove a graph as an embedded library. This section runs the
**same graph**, with a `HumanApprovalNode` spliced between the agent
and the tool, as a real FastAPI service. You will submit a run over
HTTP, watch it pause on the approval gate, and resolve it either via
`curl` (the copy-pasteable human-operator path) or via a Python HTTP
client (the programmatic path).

This is where Zeroth's governance surface becomes visible: an approval
record is persisted, queryable, and resolvable through a versioned,
deployment-scoped HTTP API.

## Booting Zeroth as a service

In production you run Zeroth as a long-lived FastAPI service with
uvicorn. The canonical command (which is what `Dockerfile` runs) is:

```bash
uv run python -m zeroth.core.service.entrypoint
```

This reads config from environment variables (`ZEROTH_DEPLOYMENT_REF`,
`ZEROTH_DATABASE__BACKEND`, `PORT`, etc.), runs Alembic migrations on
Postgres, and calls `uvicorn.run(...)` against the
`entrypoint:app_factory` factory. The factory itself wraps
`bootstrap_service(...)` — the same function the library-mode example
calls.

For the tutorial, `examples/20_approval_gate.py` boots the same service
**in-process** on a real uvicorn instance, so the curl command it
prints is the actual command an operator would run in another
terminal. Production and the tutorial use the exact same code path.

## The example script

```python title="examples/20_approval_gate.py"
--8<-- "20_approval_gate.py"
```

Run it:

```bash
python examples/20_approval_gate.py
```

## Expected output

```text
Run <uuid> status: paused_for_approval

# Equivalent curl command (Section 3 of the Getting Started tutorial):
curl -X POST http://localhost:8000/deployments/demo-approval/approvals/<approval_id>/resolve \
     -H "X-API-Key: demo-operator-key" \
     -H "Content-Type: application/json" \
     -d '{"decision": "approve"}'

Run <uuid> final status: succeeded
Final output: {'message': '<one-line greeting from the LLM>'}
```

## Approve via curl

The script prints the exact `curl` command you would run against a
live uvicorn daemon on `localhost:8000`. In a real human-in-the-loop
deployment, the operator receives the `approval_id` through a webhook
or the Studio UI, and runs this command (or clicks the equivalent
button in Studio):

```bash
curl -X POST http://localhost:8000/deployments/demo-approval/approvals/$APPROVAL_ID/resolve \
     -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"decision": "approve"}'
```

The endpoint is
`POST /deployments/{deployment_ref}/approvals/{approval_id}/resolve`.
Its request/response schemas live in
`zeroth.core.service.approval_api` and are covered by the OpenAPI spec
committed at `openapi/zeroth-core-openapi.json`. Passing
`{"decision": "reject"}` would fail the run instead.

## Service mode vs library mode

| Aspect | Library (`01_first_graph.py`) | Service (`20_approval_gate.py`) |
| --- | --- | --- |
| Transport | In-process Python calls | HTTP (FastAPI + uvicorn) |
| Approval gate | Blocks the awaited coroutine | Returns a `paused_for_approval` run; resolved out-of-band |
| Auth | None needed (in-process) | API key, JWT, or OAuth bearer (`ServiceAuthConfig`) |
| Multi-tenant | Implicit (your process) | Explicit (`tenant_id`/`workspace_id` on every request) |
| Typical use | Notebooks, tests, single-binary tools | Production deployments, Studio, webhooks, CLI operators |

The auth configuration used here is a minimal
`StaticApiKeyCredential` for the tutorial. Production deployments
should wire OAuth/JWT via `ServiceAuthConfig.from_env()`; see the
service auth documentation (Phase 32) for the full story.

## You made it

You have just:

1. Installed `zeroth-core` in a clean venv.
2. Built and run a governed graph embedded as a library.
3. Submitted a run against Zeroth's HTTP API, paused on a human
   approval gate, and resolved it through the real
   `/approvals/{id}/resolve` endpoint.

Next up is the [Governance Walkthrough](../governance-walkthrough.md),
which exercises the other two Zeroth differentiators — auditor review
of the full decision trail and policy-based tool blocking — against a
single example workflow.
