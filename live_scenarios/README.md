# Live Scenarios

## Research Audit

`research_audit` is a deployment-scoped FastAPI scenario that exercises:

- deployment snapshot bootstrapping
- agent execution
- tool calling through executable units
- wrapped-command subprocess execution
- shared thread-scoped memory
- thread continuity across runs
- approval pause/resume
- policy denial paths

## Requirements

Base setup:

```bash
uv sync
```

Live external-provider mode:

- `OPENAI_API_KEY`
- optional `OPENAI_MODEL` (default `gpt-4o-mini`)
- optional `OPENAI_BASE_URL`
- optional `LIVE_SCENARIO_SEARCH_PROVIDER`
- optional `LIVE_SCENARIO_SEARCH_API_KEY`

Without live credentials, the scenario falls back to deterministic local providers so the HTTP surface can still be exercised end to end.

## Run The Server

```bash
uv run python -m live_scenarios.research_audit.run_server --port 8011
```

Strict policy mode:

```bash
uv run python -m live_scenarios.research_audit.run_server --port 8011 --strict-policy
```

## Run Smoke Queries

```bash
uv run python -m live_scenarios.research_audit.run_queries --base-url http://127.0.0.1:8011 --auto-approve
```

Custom queries:

```bash
uv run python -m live_scenarios.research_audit.run_queries --base-url http://127.0.0.1:8011 --auto-approve "Find likely bugs in the service bootstrap"
```
