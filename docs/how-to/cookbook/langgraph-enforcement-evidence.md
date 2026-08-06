# LangGraph enforcement evidence and classification

## What this page documents
How the LangGraph gateway classifies a run or a deployment into a
governance level, what evidence that classification is computed from,
and the two knobs — the stale threshold and the adapter version — that
gate it, plus the HTTP surface that submits and reads that evidence.

The routes are registered by
`src/zeroth/service/api/enforcement_api.py` under both `/v1/enforcement`
and the compatibility prefix:

| Route | Purpose |
|---|---|
| `POST /enforcement/decisions` | Decide one tool call; one decision is *stored* per idempotency key. |
| `POST /enforcement/registrations` | Declare a deployment's governed tool inventory. |
| `POST /enforcement/attestations` | Record a run's signed start-of-run claims. |
| `POST /enforcement/heartbeats` | Report deployment liveness. |
| `GET /enforcement/deployments/{deployment_ref}/status` | Last-known deployment level. |
| `GET /enforcement/deployments/{deployment_ref}/runs/{correlation_id}` | The level provable for one run. |

All six require the `ENFORCEMENT_REPORT` permission, and every one
resolves `tenant_id` and `principal_id` from the authenticated
principal — never from the request body.

## When to use
- You are building or auditing the evidence pipeline behind
  `CapabilityReporter`, `PersistedCapabilityEvidenceProvider`, or the
  run-attestation contracts, and need to know which combination of
  facts actually produces `enforced`.
- You are debugging why a run reports `observed` when you expected
  `enforced` — the checklist in
  [What `enforced` requires](#what-enforced-requires) is the fastest
  way to find the failing condition.
- You are writing or reviewing policy/ops language that references a
  "stale" run or deployment and need the exact threshold.

## When NOT to use
- You want to *govern* tool calls — that is
  [Govern LangGraph tool calls](govern-langgraph-tools.md), a
  different recipe. This page documents how a run's or a deployment's
  governance level is **reported**, not how an individual tool call is
  decided.
- You want to *submit* evidence from an adapter rather than understand
  how it is judged. The three write routes above —
  `POST /enforcement/registrations`, `POST /enforcement/attestations`
  and `POST /enforcement/heartbeats` — are the surface, and an adapter
  calls them **directly**: authenticate with `X-API-Key` and POST the
  bodies defined in `src/zeroth/governance/enforcement_wire.py`
  (`InventorySubmission`, `AttestationSubmission`,
  `HeartbeatSubmission`). There is no SDK evidence client today.
  `zeroth.integrations.langgraph.HttpToolDecisionClient` is **not**
  one: it posts to `/v1/enforcement/decisions` and nothing else, so it
  can neither register an inventory, nor attest a run start, nor
  heartbeat.

## The three levels, and why there is no "partial" level
`GovernanceLevel` (`src/zeroth/contracts/langgraph_gateway/models.py:13`) has
exactly three members:

- **`admission`** — the run or deployment passed through the gateway,
  but nothing further is claimed. This is the default and the
  fail-closed floor: any missing, invalid, mismatched, or stale
  evidence collapses to `admission`.
- **`observed`** — evidence exists and is fresh and valid, but it does
  not establish full tool-inventory coverage tied to a matching
  attestation. The gateway is watching the run; it is not claiming to
  have governed every tool call the run could make.
- **`enforced`** — every condition in
  [What `enforced` requires](#what-enforced-requires) held at
  evaluation time.

There is no fourth, "partial" level, and this is deliberate.
`InventoryCoverage` (`src/zeroth/integrations/langgraph/_tool_types.py:69`)
is a separate two-valued enum — `partial` / `complete` — that describes
**how much of a tool inventory was seen**, not how governed a run is.
A partial inventory and an `admission`-level run are different axes
that happen to correlate in the common case (an incomplete inventory
usually accompanies weak evidence), but conflating them into one scale
would let a caller misread "we saw half the tools" as a governance
level partway between `observed` and `enforced`. It is not: partial
coverage caps you at `observed` at best (see
[Partial inventories](#partial-inventories-cap-at-observed)), it never
produces a distinct level of its own.

## What `enforced` requires
`enforced` is a conjunction, not a single flag. Reading top to bottom
through `PersistedCapabilityEvidenceProvider` and
`CapabilityReporter._validated_level`
(`src/zeroth/governance/attestations/provider.py`,
`src/zeroth/governance/langgraph_gateway/capabilities.py:75-140`), every one of
these must hold:

1. **A signed run-start attestation that verifies.**
   `verify_attestation` (`src/zeroth/governance/attestations/signing.py:68`)
   recomputes the SHA-256 digest from the payload and checks the keyed
   signature against it. An unsigned, tampered, or unverifiable
   attestation forces `admission` immediately
   (`provider.py:242-243`, `capabilities.py:60-61`).
2. **A registered inventory whose coverage is exactly `complete`.**
   `_coverage_is_complete` compares the stored registration's coverage
   against the literal string `"complete"` — no case folding, no other
   token accepted.
3. **A tool-fingerprint match between the registration and the
   attestation.** `_manifest_complete` requires
   `registration.inventory_fingerprint == payload.inventory_fingerprint`.
   A `complete` registration for the *wrong* inventory proves nothing
   about this run.

### What the registration trust boundary actually is

Be precise about which half of that comparison the server owns, because
an earlier revision of this page overstated it.

- **Server-computed.** `inventory_fingerprint` and `tool_count` on a
  *registration* are recomputed from the submitted tool identities by
  `recompute_inventory_fingerprint`
  (`src/zeroth/governance/attestations/inventory.py`) and cannot be
  declared. `inventory_coverage` and `tool_count` inside a *signed
  attestation* are taken from the stored registration, not from the
  submitted body.
- **Client-declared, and deliberately so.** The attestation's
  `inventory_fingerprint` is submitted by the run. It is a binding, not
  an authority claim: the whole check is that it equals the digest the
  server recomputed. Deriving it from the registration too would compare
  a value with itself and make fingerprint drift undetectable.
- **Client-declared, and irreducible here.** `coverage` on a
  registration is the adapter's claim that it enumerated every governed
  tool. The server has no independent view of the graph, so it cannot
  falsify that claim; what it can and does refuse is a `complete` claim
  whose declared identities do not produce the attested digest.
4. **Matching graph and adapter versions.** `classify_version_agreement`
   must return `VersionAgreement.MATCH` — see
   [Mixed versions](#mixed-versions-and-the-adapter-version).
5. **Freshness**, checked twice, on two different clocks:
   - The provider checks the attestation's own signed `expires_at`
     against its clock (`provider.py:250`) — an expired attestation
     still has a valid signature, but its consequence (a possible
     `enforced` claim) is refused.
   - `CapabilityReporter._validated_level` separately checks
     `observed_at` (the attestation's `issued_at`) against the
     **stale threshold** — see
     [The stale threshold](#the-stale-threshold-90-seconds).

If all five hold, the provider computes `governance_level=ENFORCED` and
`tool_manifest_complete=True` on the evidence it returns. Even then,
`CapabilityReporter._validated_level` (`capabilities.py:84-91`) makes
its own decision from that evidence: it only returns `ENFORCED` when
`evidence.governance_level is GovernanceLevel.ENFORCED` **and**
`evidence.tool_manifest_complete` are both true; otherwise it returns
`OBSERVED`. Nothing here is copied through — `RunAttestationPayload
.claimed_level` (`src/zeroth/governance/attestations/payload.py:66-72`)
is the run's own advisory claim, and it can only ever **lower** the
level the server independently computed, never raise it
(`_apply_ceiling`, `provider.py:103-105`). **The server recomputes this
from the inventory it stored** for the deployment, never from anything
the client asserts about its own coverage.

## The stale threshold: 90 seconds
`CapabilityReporter` is constructed with `stale_after_seconds`, which
defaults to **90.0** seconds
(`src/zeroth/governance/langgraph_gateway/capabilities.py:44`). It is
configurable through `LangGraphGatewaySettings.stale_threshold_seconds`,
which also defaults to `90`
(`src/zeroth/platform/config/settings.py:322`).

`_validated_level` computes `age_seconds` as the gap between the
reporter's clock and the evidence's `observed_at`
(`capabilities.py:74-82`):

- If `age_seconds` is negative (clock skew), infinite/NaN, or greater
  than `stale_after_seconds`, the evidence is treated as unusable and
  the result is `admission` — regardless of how strong the evidence
  would otherwise be. **Evidence older than 90 seconds (by default)
  downgrades the reported level to `admission`, full stop** — this is
  the "documented threshold" a stale heartbeat is measured against.
- At exactly `stale_after_seconds` the evidence is still fresh (the
  comparison is `age_seconds > stale_after_seconds`, not `>=`), so a
  reading taken at precisely the 90-second mark still counts.
- Otherwise the age check passes and evaluation continues to the level
  logic described above.

This window is what "stale" means throughout the gateway: evidence
older than the threshold is discarded outright, not downgraded
gracefully to some intermediate state. The same check backs both call
sites — `level_for_run` and `level_for_deployment`. A run's own
`observed_at` is its attestation's `issued_at`, set once at run start;
by construction this means a long-running run's evidence keeps aging
toward the same 90-second boundary unless a fresh attestation is
issued, in parallel with the attestation's own `expires_at` TTL
enforced separately in `PersistedCapabilityEvidenceProvider._resolve_level`
(`provider.py:250`) — the two are independent gates on different
clocks, and neither is a substitute for the other. (This consequence
for long-running runs follows from reading `_validated_level` and
`evidence_for_run` together; it is not something this page saw pinned
by a dedicated test, so treat it as a derived reading of the code
rather than a directly-tested guarantee.)

## Heartbeats can report deployment status, never upgrade a run
A heartbeat is evidence about a **deployment**, not about any one run.
`zeroth.governance.attestations.heartbeat` implements this concretely:

- `Heartbeat` (`heartbeat.py:92-111`) is one liveness ping — a plain,
  **unsigned** record: `tenant_id`, `deployment_ref`, an optional
  `graph_version`/`adapter_version`, an `observed_at` timestamp, and a
  self-reported `reported_level` string. `HeartbeatRepository`
  (`heartbeat.py:137-200`) appends these to the `enforcement_heartbeats`
  table and reads back only the newest row per `(tenant_id,
  deployment_ref)` — last-known-wins.
- `DeploymentStatusResolver.last_known_evidence()`
  (`heartbeat.py:240-276`) turns the newest heartbeat into a
  `RunCapabilityEvidence` and hands it to
  `CapabilityReporter.level_for_deployment`
  (`capabilities.py:114-121`), which applies the same
  `_validated_level` staleness window described above — the staleness
  rule is not reimplemented here, it lives in exactly one place.
  `DEFAULT_STALE_AFTER_SECONDS` (`heartbeat.py:60-68`) is read from
  `LangGraphGatewaySettings.stale_threshold_seconds` rather than
  duplicated as a literal, so the two can never drift apart on what
  "stale" means.
- The evidence this resolver builds always carries
  `tool_manifest_complete=False` (`heartbeat.py:243-250`) — a
  heartbeat proves no tool inventory, so no heartbeat, however fresh
  and however it self-reports, can ever satisfy the `ENFORCED`
  predicate in `_validated_level`. `signature_valid` is always `True`
  on this evidence, but that is not a cryptographic claim: a
  heartbeat's trust boundary is the tenant-scoped read itself, not a
  signature over its payload, and it is `tool_manifest_complete` that
  stops an over-claiming heartbeat from reaching `ENFORCED`, not this
  flag.

`level_for_run` (`capabilities.py:95-112`) takes an optional
`deployment_evidence` parameter, and its first line is `del
deployment_evidence` (`capabilities.py:103`). The heartbeat evidence is
accepted for interface symmetry and then discarded by construction — a
run's level is computed solely from `evidence_for_run(correlation_id)`,
its own attestation lookup. A deployment that just heartbeated
`enforced` cannot lend that status to a run that has no attestation of
its own, or whose attestation fails any of the five conditions above.
Heartbeats answer "is the deployment currently healthy and governed",
not "is this particular run enforced".

## Mixed versions and the adapter version
`ADAPTER_VERSION` (`src/zeroth/governance/attestations/versions.py:41`)
identifies the governed-LangGraph adapter's own attestation/registration
wire contract — a different clock from the repository's release
version in `pyproject.toml`. It only moves when the *shape or meaning*
of what the adapter attests or registers changes, not on every release.

`classify_version_agreement`
(`src/zeroth/governance/attestations/versions.py:63-109`) compares two
independent pairs — expected vs. actual graph version, and expected vs.
actual **adapter version** — and returns a `VersionAgreement` member:

| Member | Meaning |
| --- | --- |
| `MATCH` | Both the graph version and the adapter version agree exactly. |
| `ADAPTER_MISMATCH` | Graph versions agree; adapter versions do not. |
| `GRAPH_MISMATCH` | Adapter versions agree; graph versions do not. |
| `BOTH_MISMATCH` | Neither pair agrees. |
| `UNKNOWN` | Any one of the four inputs was `None` or `""`. |

Comparison is **exact string equality on both axes — there is no
semver range or compatibility logic**. This is on purpose: a
governance claim ("this run is fully enforced") must not depend on
someone's interpretation of what counts as a compatible version range.
If a wire-contract change is genuinely compatible, that is expressed by
*not* bumping `ADAPTER_VERSION` — not by teaching the comparison to
reason about ranges.

`UNKNOWN` is checked **before** either pair is compared: if the
expected or actual graph version, or the expected or actual adapter
version, is missing or empty, the result is `UNKNOWN` regardless of
what the other pair would have shown. **A missing version is not
evidence of agreement — it is the absence of the evidence agreement
would require**, so it fails closed to `UNKNOWN` rather than falling
through to a mismatch or a match.

`permits_full_enforcement` (`versions.py:112-130`) is the single gate a
caller checks: **every value other than `MATCH` — including
`UNKNOWN` — forbids a full-enforcement claim.** It is written as an
explicit equality check against `MATCH`, not as an allowlist or
denylist of the other members, so a future `VersionAgreement` member
added without updating this function still forbids enforcement by
default instead of silently permitting it.

## Partial inventories cap at `observed`
An inventory with `InventoryCoverage.PARTIAL` can still identify and
back specific controlled tools — the gateway is not blind to a
partially-inventoried deployment — but it can never, by itself, satisfy
condition 2 in
[What `enforced` requires](#what-enforced-requires), which requires
coverage to be exactly `complete`. A run backed by a partial inventory
therefore reports at most `observed`, no matter how strong the rest of
its evidence is: a valid signature, a fingerprint match on the partial
set, and a `MATCH` version agreement all still land on `OBSERVED`
because `_manifest_complete` returns `False` for anything short of
`complete` coverage (`provider.py:77-79`, `204-225`).

This mirrors the same distinction drawn in
[The three levels](#the-three-levels-and-why-there-is-no-partial-level):
`partial` is a fact about the inventory, and its consequence is a
governance-level ceiling, not a governance level of its own.

## Attesting twice: what a 409 means and what a retry gets
A run's evidence is fixed by the first attestation the server accepts:
`run_attestations` is unique on `(tenant_id, correlation_id)`, so a
second attestation under one correlation cannot replace it. An adapter
therefore has to be able to tell three cases apart.

| What the adapter sent | Answer | `authoritative` |
|---|---|---|
| The same claims again (a retry after a lost response) | `201` with the **original** digest and expiry | `true` |
| Different claims under the same correlation | `409` with the digest and expiry of the attestation in force | `false` |
| A correlation another deployment already attested | `409`, fixed message, no digest | — |

**A retry is safe and costs nothing.** The server stamps `issued_at` and
`expires_at` on arrival, so a retried request is normally *not*
byte-identical to the one that was stored, and neither its digest nor its
signature can identify it. (The exception is two requests landing in the
same microsecond, which do produce identical bytes; that duplicate is
recognised too, and counted separately.) `is_identical_resubmission`
(`src/zeroth/governance/attestations/signing.py`) instead compares every
claim except that issuance window and re-signs the stored payload to
confirm it is intact and still what the current key produces. A matching
retry gets the first response back verbatim and writes no second row; an
adapter can safely POST again after a timeout without deciding whether
the first attempt landed.

**Three things break the match, all deliberately.** Changed claims —
including `inventory_coverage` and `tool_count`, which the server takes
from the deployment's *current* registration, so a retry crossing a
re-registration that changed either one is a different attestation. A
rotated or retired signing key. And a change in whether the deployment
signs at all. Each answers `409`: the attestation in force is not the one
this request would have produced, and saying otherwise would tell the
adapter its claims govern a run they do not.

**A re-registration on its own is not enough to break it.** Swapping one
tool for another leaves the coverage and the count unchanged, and the
`inventory_fingerprint` in the comparison is the adapter's own submitted
value — identical across both requests by definition. That retry still
matches, and still gets the original acceptance. The fingerprint is
checked against the server's recomputed digest when the evidence is
*read* (see [What `enforced` requires](#what-enforced-requires)), not
when a duplicate attestation is judged.

The third row of the table is narrower on purpose. Correlations are
unique per *tenant*, not per deployment, so a submission can lose to a
sibling deployment's run; disclosing that winner's digest would make the
409 body a read of another deployment's evidence, so that case answers a
fixed message instead.

On the metrics side the three outcomes are distinct label values on
`zeroth_enforcement_attestations_total` — `recorded`, `already_recorded`,
and `earlier_attestation_in_force` (plus `unavailable`). A retry is
deliberately indistinguishable on the wire, so the counter is where an
operator sees retry volume, and `recorded` stays an honest count of
attestations actually stored.

## See also
- [Govern LangGraph tool calls](govern-langgraph-tools.md) — the
  tool-call enforcement surface this evidence model is distinct from;
  see its ["What this does not claim"](govern-langgraph-tools.md#what-this-does-not-claim)
  section for how the two relate.
- [Concept: guardrails](../../concepts/guardrails.md)
- [Concept: audit](../../concepts/audit.md)
