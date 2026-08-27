# Live Studio evaluation

This campaign evaluates Studio, runtime execution, Audit, Cost, and Regulus before
Zeroth Check. The non-secret profile is
`release/live_evaluation/campaign-v1.json`.

## Safety boundary

- Tenant and campaign: `evaluation-studio-v1`
- Chat model: `openai/gpt-4o-mini`
- Embeddings: `openai/text-embedding-3-small`
- Vector backend: dedicated Chroma on loopback
- Tenant/campaign ceiling: `$10.00`
- Per-run ceiling: `$0.25`
- Action target: the campaign-local SQLite action sink only

Campaign-tagged chat and embedding calls reserve a conservative maximum before the
provider call. Admission is atomic across concurrent branches. Success commits the
measured or estimated cost and releases the remainder. Timeouts, cancellations,
missing usage, and ambiguous outcomes retain the maximum until explicit
reconciliation. The evaluation-only service rejects billable run and probe requests
that omit the configured campaign identity.

The provider credential resolves as logical secret `llm.openai` through Zeroth's
tenant-scoped secret provider. Never put a concrete key in the campaign profile,
repository, command arguments, browser storage artifacts, or evidence bundle. The
credential pasted into chat is excluded.

## Evidence lifecycle

`initialize_control_plane_evidence(...)` creates a unique external bundle containing
the frozen revision/diff, database snapshots, runtime/browser/container versions, and
the complete acceptance catalog. `CampaignCoordinator` executes pluggable stages in
strict order and resumes from `events.ndjson` without replaying completed actions.

All command output and exit codes are persisted. Browser output must enter the bundle
through `EvidenceStore.ingest_artifact`; direct Playwright output is staging material,
not accepted evidence. Finalization validates every evidence reference, recursively
scans text and supported binary files for secret shapes, writes acceptance/report,
and seals the bundle with `SHA256SUMS`. After sealing, the evidence API refuses all
mutation. Binary scanning does not provide OCR, so screenshots and videos still need
the DOM/URL secret-shape gate and visual review.

## Control-plane gate

Do not make a provider call until all of these are green:

1. Baseline unit, integration, architecture, frontend, build, and sanitizer tests.
2. Pretest SQLite snapshots and revision/diff capture.
3. Tenant-scoped provider and HMAC signing secrets injected outside the repository.
4. Audit readiness reports `state=signed`.
5. `$10` tenant and `$0.25` run limits are stored and concurrency, rejection,
   commit/release, ambiguity, and recovery tests pass.
6. Chroma is pinned to `chromadb/chroma:1.5.6`, bound to loopback, and seeded with
   three hashed synthetic documents.
7. Exactly one newly instrumented provider verification and one instrumented Chroma
   probe reconcile across reservation, Audit, and Regulus records.

The Chroma probe performs one embedding-producing write. Its operation is
reserved before the connector call and committed from the embedding response's
usage and provider-request identity. Backend failure before the embedding
boundary releases the reservation as `provider_not_called`; timeout or an
unknown provider outcome remains reserved for reconciliation.

Prior uninstrumented connectivity probes are historical diagnostics only. They do not
count toward this campaign's required instrumented probe pair or acceptance evidence.

## Execution order

Run the campaign serially: control gate, grounded researcher, batched investigation,
governed remediation, cross-cutting UI/resilience, then Zeroth Check. Each workflow
requires three successful happy-path repetitions and every registered negative case.
Any stop-condition failure halts immediately and leaves later criteria blocked.

For the action workflow, start the evaluation-only service after deploying the exact
graph version:

```bash
uv run python -m release.live_evaluation.service \
  --campaign-config release/live_evaluation/campaign-v1.json \
  --deployment-ref <workflow-3-deployment-ref> \
  --host 127.0.0.1 \
  --port 8120
```

The service fails startup unless signed audit is active and the configured action sink
is used. It never targets payment, email, production, or third-party action APIs.

## Known operational risks

- HMAC signing proves keyed local integrity, not third-party non-repudiation.
- Provider-SDK-internal retries are opaque below one reserved logical call.
- Regulus delivery is not transactionally outboxed with reservation settlement; the
  campaign must halt if the exactly-one event reconciliation fails.
- The approved source `.env` currently has mode `0644`; inject its value only into the
  evaluation process and tighten the source file permissions separately.
- A dirty worktree can fail the wheel tracked-source architecture test even when
  runtime safety tests pass. Do not stage or commit unrelated user work merely to
  manufacture a green packaging result.
