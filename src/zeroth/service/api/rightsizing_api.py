"""Model right-sizing REST API (ECON-RIGHTSIZE-01).

Exposes the design-time nudge the studio inspector shows under a node's model field:
cheaper, capability-compatible alternatives to the model the user picked. Pure lookup
over litellm's model DB — no Regulus, no network — so it works even when cost tracking
is not configured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter
from zeroth.econ.analytics.identity import authored_node_id, stable_runtime_node_id
from zeroth.econ.analytics.opportunities import SpendReport, spend_opportunities
from zeroth.econ.analytics.quality import read_quality_verdict
from zeroth.econ.analytics.rightsizing import RightsizingResult, describe, recommend
from zeroth.econ.analytics.rightsizing_experiment import (
    ExperimentCallEvidence,
    ExperimentExecutionEvidence,
    ExperimentReport,
    build_experiment_dataset,
    build_labeled_dataset,
    run_experiment,
)
from zeroth.governance.audit.models import AuditQuery, NodeAuditRecord, TokenUsage
from zeroth.governance.audit.readiness import signer_is_available
from zeroth.governance.audit.repository import AuditRepository
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents.provider import LiteLLMProviderAdapter
from zeroth.runtime.runs import Run, RunFailureState, RunHistoryEntry, RunStatus
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)


class RightsizingRequest(BaseModel):
    """Request body for POST /v1/econ/rightsizing.

    Task-shape flags are *capability gates*, derived from how the node is wired: an agent
    with tools attached needs ``needs_tools``; one handed images needs ``needs_vision``.
    """

    model_config = ConfigDict(extra="forbid")

    incumbent: str = Field(min_length=1, description="Current model, e.g. openai/gpt-4o")
    needs_tools: bool = False
    needs_vision: bool = False
    min_savings_pct: float = Field(default=20.0, ge=0.0, le=100.0)
    limit: int = Field(default=6, ge=1, le=20)


#: Upper bound on the audit history either right-sizing route reads. Both are
#: rankings over recent spend, so the newest slice is what they need; without a
#: bound they read the deployment's entire audit trail on every request.
_AUDIT_READ_BOUND = 10_000
_RUN_READ_BOUND = 10_000


def _completed(run: object) -> bool:
    return str(getattr(run, "status", "")).upper().removeprefix("RUNSTATUS.") == "COMPLETED"


def _stored_experiment_report(run: object) -> ExperimentReport | None:
    """Return a validated measured report from a durable Rightsizing run."""
    if getattr(run, "metadata", {}).get("execution_kind") != "rightsizing_experiment":
        return None
    final_output = getattr(run, "final_output", None)
    if not isinstance(final_output, dict):
        return None
    payload = final_output.get("rightsizing_experiment_report")
    if not isinstance(payload, dict):
        return None
    try:
        return ExperimentReport.model_validate(payload)
    except ValueError:
        return None


async def _composed_deployment_records(
    run_repository: RunRepository,
    audit_repository: AuditRepository,
    deployment_ref: str | None,
) -> tuple[list[NodeAuditRecord], list[object]]:
    """Read active-deployment runs plus their durable composed descendants.

    Subgraph children keep their own deployment identity, so a deployment-only
    audit query omits the model calls that the active parent orchestrated.  The
    persisted ``parent_run_id`` relation is the authoritative traversal edge.
    Repositories are already tenant/workspace scoped; the visited set and global
    bound keep malformed or cyclic lineage fail-safe.
    """
    roots = list(
        await run_repository.list_runs(deployment_ref, limit=_RUN_READ_BOUND)
    )
    runs: list[object] = list(roots)
    visited = {str(run.run_id) for run in roots}
    pending = list(roots)
    list_children = getattr(run_repository, "list_child_runs", None)
    if callable(list_children):
        while pending and len(runs) < _RUN_READ_BOUND:
            parent = pending.pop(0)
            for child in await list_children(str(parent.run_id)):
                child_id = str(child.run_id)
                if child_id in visited:
                    continue
                visited.add(child_id)
                runs.append(child)
                pending.append(child)
                if len(runs) >= _RUN_READ_BOUND:
                    break

    records = list(
        await audit_repository.list(
            AuditQuery(deployment_ref=deployment_ref),
            limit=_AUDIT_READ_BOUND,
        )
    )
    seen_audits = {record.audit_id for record in records}
    root_ids = {str(run.run_id) for run in roots}
    for run in runs:
        run_id = str(run.run_id)
        if run_id in root_ids:
            continue
        for record in await audit_repository.list(
            AuditQuery(run_id=run_id),
            limit=_AUDIT_READ_BOUND,
        ):
            if record.audit_id in seen_audits:
                continue
            seen_audits.add(record.audit_id)
            records.append(record)
    return records, runs


class ExperimentRequest(BaseModel):
    """Request body for POST /v1/econ/rightsizing/experiment (measured right-sizing).

    Replays this node's real audit-trail inputs through cheaper candidate models and scores
    equivalence to the incumbent. Runs real LLM calls, so the sample is capped small — the
    result is a *flagged* lead below ``min_cases``, not a *confirmed* switch.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    source_deployment_ref: str | None = Field(default=None, min_length=1)
    incumbent: str = Field(min_length=1, description="Current model, e.g. openai/gpt-4o")
    instruction: str = Field(min_length=1, description="The agent's system instruction")
    needs_tools: bool = False
    needs_vision: bool = False
    judge_model: str | None = None  # defaults to the incumbent — a model the user trusts
    max_candidates: int = Field(default=2, ge=1, le=6)
    max_cases: int = Field(default=5, ge=1, le=25)
    min_cases: int = Field(default=5, ge=1, le=50)
    tolerance_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    # "equivalence" grades a candidate against the incumbent's own output (label-free, but
    # inherits its mistakes). "correctness" grades against human-provided expected answers
    # (attached via POST /econ/quality-verdict) — the honest bar for high-stakes nodes.
    mode: Literal["equivalence", "correctness"] = "equivalence"


def _experiment_node_records(
    records: list[NodeAuditRecord],
    *,
    node_id: str,
    source_deployment_ref: str | None,
) -> tuple[list[NodeAuditRecord], list[str]]:
    """Resolve a dashboard node selector to its replayable runtime records.

    A plain authored ID (for example ``analyze``) intentionally folds parallel
    branch/subgraph namespaces, but only within one source deployment. An old
    branch-qualified selector remains compatible and folds sibling branches of
    the same subgraph-qualified runtime node.
    """
    requested_authored_id = authored_node_id(node_id)
    if requested_authored_id == node_id:
        matched = [
            record
            for record in records
            if authored_node_id(record.node_id) == requested_authored_id
        ]
    else:
        requested_runtime_id = stable_runtime_node_id(node_id)
        matched = [
            record
            for record in records
            if stable_runtime_node_id(record.node_id) == requested_runtime_id
        ]

    if source_deployment_ref is not None:
        matched = [
            record
            for record in matched
            if record.deployment_ref == source_deployment_ref
        ]
        return matched, []

    deployment_refs = sorted({record.deployment_ref for record in matched})
    if len(deployment_refs) > 1:
        return [], deployment_refs
    return matched, []


def _with_run_history_snapshots(
    records: list[NodeAuditRecord], runs: list[object]
) -> list[NodeAuditRecord]:
    """Join replay payloads from durable run history onto signed audit identity.

    Audit capture can intentionally omit payloads while retaining provider/model,
    cost, and signature evidence. The scoped run repository remains the durable
    replay source. This returns in-memory copies only; signed audit rows are never
    rewritten or re-chained.
    """
    histories: dict[str, list[object]] = {
        str(run.run_id): list(getattr(run, "execution_history", ()) or ())
        for run in runs
    }
    enriched: list[NodeAuditRecord] = []
    for record in records:
        if record.input_snapshot and record.output_snapshot:
            enriched.append(record)
            continue
        entries = [
            entry
            for entry in histories.get(record.run_id, ())
            if getattr(entry, "node_id", None) == record.node_id
        ]
        if not entries:
            enriched.append(record)
            continue
        audit_ref = record.audit_id.removeprefix(f"{record.run_id}:")
        entry = next(
            (
                candidate
                for candidate in entries
                if getattr(candidate, "audit_ref", None) == audit_ref
            ),
            entries[-1],
        )
        input_snapshot = getattr(entry, "input_snapshot", None)
        output_snapshot = getattr(entry, "output_snapshot", None)
        enriched.append(
            record.model_copy(
                update={
                    "input_snapshot": (
                        record.input_snapshot
                        or (dict(input_snapshot) if isinstance(input_snapshot, dict) else {})
                    ),
                    "output_snapshot": (
                        record.output_snapshot
                        or (dict(output_snapshot) if isinstance(output_snapshot, dict) else {})
                    ),
                }
            )
        )
    return enriched


def register_rightsizing_routes(app: FastAPI | APIRouter) -> None:
    """Register model right-sizing routes on the FastAPI app."""

    @app.post("/econ/rightsizing", response_model=RightsizingResult)
    async def suggest_rightsizing(request: Request, body: RightsizingRequest) -> RightsizingResult:
        """Return cheaper, capability-compatible alternatives to ``body.incumbent``.

        Candidates to A/B test, not a verdict — the endpoint gates on capability and
        price only. An unknown incumbent returns ``incumbent_known=False`` (a 200 with an
        explanation), never an error.
        """
        await require_permission(request, Permission.WORKFLOW_READ)
        return recommend(
            body.incumbent,
            needs_tools=body.needs_tools,
            needs_vision=body.needs_vision,
            min_savings_pct=body.min_savings_pct,
            limit=body.limit,
        )

    @app.get("/econ/rightsizing/opportunities", response_model=SpendReport)
    async def rightsizing_opportunities(request: Request) -> SpendReport:
        """Rank this deployment's agent nodes by right-sizing opportunity (spend × savings).

        Read-only aggregation over the audit trail — no LLM calls. Surfaces which nodes are
        worth a measured experiment. Returns an empty report (200) when no spend is on record.
        """
        await require_permission(request, Permission.METRICS_READ)
        bootstrap = getattr(request.app.state, "bootstrap", None)
        if bootstrap is None or getattr(bootstrap, "audit_repository", None) is None:
            raise HTTPException(status_code=503, detail="audit repository not configured")
        run_repository = getattr(bootstrap, "run_repository", None)
        if run_repository is None:
            raise HTTPException(status_code=503, detail="run repository not configured")
        deployment = getattr(bootstrap, "deployment", None)
        await require_deployment_scope(request, deployment)
        deployment_ref = getattr(deployment, "deployment_ref", None)
        records, runs = await _composed_deployment_records(
            run_repository,
            bootstrap.audit_repository,
            deployment_ref,
        )
        completed_run_ids = {str(run.run_id) for run in runs if _completed(run)}
        return spend_opportunities(records, eligible_run_ids=completed_run_ids)

    @app.post("/econ/rightsizing/experiment", response_model=ExperimentReport)
    async def run_rightsizing_experiment(
        request: Request, body: ExperimentRequest
    ) -> ExperimentReport:
        """Replay this node's real inputs through cheaper models and measure equivalence.

        Every branch returns a 200 with an explanatory ``note`` rather than an error: an
        unpriced incumbent, no cheaper candidates, no run history, or a missing provider key
        are all *results* of the measurement, not failures of the endpoint.
        """
        principal = await require_permission(request, Permission.METRICS_ADMIN)
        bootstrap = getattr(request.app.state, "bootstrap", None)
        if bootstrap is None or getattr(bootstrap, "audit_repository", None) is None:
            raise HTTPException(status_code=503, detail="audit repository not configured")
        deployment = getattr(bootstrap, "deployment", None)
        await require_deployment_scope(request, deployment)
        deployment_ref = getattr(deployment, "deployment_ref", None)

        incumbent_option = describe(body.incumbent)
        if incumbent_option is None:
            return ExperimentReport(
                incumbent=body.incumbent,
                node_id=body.node_id,
                note=(
                    f"No pricing on record for {body.incumbent!r} — can't run a measured "
                    "comparison against it yet."
                ),
            )

        rec = recommend(
            body.incumbent,
            needs_tools=body.needs_tools,
            needs_vision=body.needs_vision,
            limit=body.max_candidates,
        )
        if not rec.candidates:
            return ExperimentReport(
                incumbent=incumbent_option.model, node_id=body.node_id, note=rec.note
            )

        deployment_records, composed_runs = await _composed_deployment_records(
            bootstrap.run_repository,
            bootstrap.audit_repository,
            deployment_ref,
        )
        records, ambiguous_deployments = _experiment_node_records(
            deployment_records,
            node_id=body.node_id,
            source_deployment_ref=body.source_deployment_ref,
        )
        if ambiguous_deployments:
            return ExperimentReport(
                incumbent=incumbent_option.model,
                node_id=body.node_id,
                mode=body.mode,
                note=(
                    "This authored node ID exists in multiple composed deployments "
                    f"({', '.join(ambiguous_deployments)}). Select a deployment-specific "
                    "opportunity before running a measured experiment."
                ),
            )
        records = _with_run_history_snapshots(records, composed_runs)
        if body.mode == "correctness":
            # Grade against human-labeled answers: harvest the deployment's runs, keep those
            # whose quality verdict carries an expected answer, and grade the candidate on
            # correctness against that ground truth (not the incumbent's own output).
            run_repository = getattr(bootstrap, "run_repository", None)
            if run_repository is None:
                return ExperimentReport(
                    incumbent=incumbent_option.model,
                    node_id=body.node_id,
                    mode="correctness",
                    note="Correctness grading needs the run repository, which isn't configured.",
                )
            expected_by_run: dict[str, str] = {}
            for run in composed_runs[:500]:
                verdict = read_quality_verdict(run)
                if verdict is not None and verdict.expected_output:
                    expected_by_run[run.run_id] = verdict.expected_output
            dataset, harvest = build_labeled_dataset(
                records,
                expected_by_run,
                incumbent_model=body.incumbent,
                max_cases=body.max_cases,
            )
        else:
            dataset, harvest = build_experiment_dataset(
                records, incumbent_model=body.incumbent, max_cases=body.max_cases
            )

        secret_provider = getattr(bootstrap, "secret_provider", None)
        provider = LiteLLMProviderAdapter(
            secret_provider=secret_provider,
            tenant_id=principal.tenant_id,
            # A paid experiment must never escape the tenant secret provider
            # into process-global provider environment variables.
            allow_env_fallback=False,
        )
        instrumented_provider: InstrumentedProviderAdapter | None = None
        experiment_run_id: str | None = None
        synthetic_run: Run | None = None
        campaign_id = getattr(bootstrap, "evaluation_campaign_id", None)
        if dataset.cases:
            instrumentation = getattr(bootstrap, "probe_instrumentation", None)
            cost_estimator = getattr(bootstrap, "cost_estimator", None)
            orchestrator = getattr(bootstrap, "orchestrator", None)
            per_run_cap_usd = getattr(orchestrator, "per_run_cap_usd", None)
            run_repository = getattr(bootstrap, "run_repository", None)
            signer = getattr(bootstrap, "signer", None)
            audit_signer = getattr(bootstrap.audit_repository, "_signer", None)
            missing = [
                name
                for name, value in (
                    ("secret provider", secret_provider),
                    ("persistent cost instrumentation", instrumentation),
                    ("cost estimator", cost_estimator),
                    ("per-run cost ceiling", per_run_cap_usd),
                    ("run repository", run_repository),
                    ("campaign identity", campaign_id),
                )
                if value is None
            ]
            if not callable(getattr(run_repository, "create", None)) or not callable(
                getattr(run_repository, "put", None)
            ):
                missing.append("writable run repository")
            if not signer_is_available(signer) or not signer_is_available(audit_signer):
                missing.append("audit signing")
            if missing:
                raise HTTPException(
                    status_code=503,
                    detail=("rightsizing experiment is fail-closed; missing " + ", ".join(missing)),
                )
            experiment_run_id = f"rightsizing:{uuid4().hex}"
            synthetic_run = Run(
                run_id=experiment_run_id,
                graph_version_ref=getattr(deployment, "graph_version_ref", "service"),
                deployment_ref=deployment_ref or "unknown",
                tenant_id=principal.tenant_id,
                workspace_id=principal.workspace_id,
                submitted_by=principal.to_actor(),
                status=RunStatus.FAILED,
                failure_state=RunFailureState(
                    reason="rightsizing_experiment_incomplete",
                    message="The measured experiment has not completed.",
                ),
                metadata={
                    "campaign_id": campaign_id,
                    "campaign_strict": True,
                    "dispatchable": False,
                    "execution_kind": "rightsizing_experiment",
                },
            )
            try:
                persisted_run = await run_repository.create(synthetic_run)
            except Exception:
                raise HTTPException(
                    status_code=503,
                    detail="rightsizing experiment is fail-closed; run persistence failed",
                ) from None
            if persisted_run.run_id != experiment_run_id:
                raise HTTPException(
                    status_code=503,
                    detail="rightsizing experiment is fail-closed; run identity changed",
                )
            instrumented_provider = InstrumentedProviderAdapter(
                inner=provider,
                regulus_client=getattr(bootstrap, "regulus_client", None),
                cost_estimator=cost_estimator,
                node_id=f"rightsizing:{body.node_id}",
                run_id=experiment_run_id,
                tenant_id=principal.tenant_id,
                deployment_ref=deployment_ref or "unknown",
                workflow_version=synthetic_run.graph_version_ref,
                cost_instrumentation=instrumentation,
                campaign_id=campaign_id,
                per_run_cap_usd=per_run_cap_usd,
                branch_id="experiment",
            )
            provider = instrumented_provider
        experiment_failed = False
        try:
            report = await run_experiment(
                incumbent=incumbent_option,
                candidates=rec.candidates,
                dataset=dataset,
                instruction=body.instruction,
                replay_provider=provider,
                judge_provider=provider,
                judge_model=body.judge_model or body.incumbent,
                mean_input_tokens=harvest.mean_input_tokens,
                mean_output_tokens=harvest.mean_output_tokens,
                harvest=harvest,
                node_id=body.node_id,
                tolerance_pct=body.tolerance_pct,
                min_cases=body.min_cases,
                mode=body.mode,
            )
        except BaseException:
            experiment_failed = True
            raise
        finally:
            if instrumented_provider is not None and experiment_run_id is not None:
                written_audits = await _write_rightsizing_call_audits(
                    audit_repository=bootstrap.audit_repository,
                    calls=instrumented_provider.call_evidence,
                    run_id=experiment_run_id,
                    campaign_id=campaign_id,
                    node_id=body.node_id,
                    deployment=deployment,
                    actor=principal.to_actor(),
                )
                if experiment_failed:
                    if synthetic_run is None:  # pragma: no cover - construction invariant
                        raise RuntimeError("rightsizing run identity is unavailable")
                    failed_run = synthetic_run.model_copy(
                        update={
                            "failure_state": RunFailureState(
                                reason="rightsizing_experiment_failed",
                                message="The measured experiment failed.",
                            ),
                            "execution_history": _rightsizing_history(written_audits),
                            "audit_refs": [record.audit_id for record in written_audits],
                            "final_output": {
                                "provider_call_count": len(written_audits),
                                "verdict": "failed",
                            },
                        }
                    )
                    try:
                        await bootstrap.run_repository.put(failed_run)
                    except Exception:
                        raise RuntimeError("rightsizing failed-run persistence failed") from None
        if instrumented_provider is None or experiment_run_id is None:
            return report
        attempted_calls = [
            item
            for item in instrumented_provider.call_evidence
            if item.provider_call_attempted and not item.cache_hit
        ]
        if len(attempted_calls) != len(written_audits):  # pragma: no cover - helper contract
            raise RuntimeError("rightsizing provider-call audit count changed")
        audit_by_operation = {
            item.operation_id: record
            for item, record in zip(attempted_calls, written_audits, strict=True)
        }
        calls = [
            ExperimentCallEvidence(
                operation_id=item.operation_id,
                provider_request_id=item.provider_request_id,
                cost_event_id=item.cost_event_id,
                audit_event_id=(
                    audit_by_operation[item.operation_id].audit_id
                    if item.provider_call_attempted and not item.cache_hit
                    else None
                ),
                model=item.model_name,
                cost_measurement=item.cost_measurement.value,
                measured_cost_usd=(
                    float(item.measured_cost_usd) if item.measured_cost_usd is not None else None
                ),
                estimated_cost_usd=(
                    float(item.estimated_cost_usd) if item.estimated_cost_usd is not None else None
                ),
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                cleanup_status=item.cleanup_status,
                provider_call_attempted=item.provider_call_attempted,
                cache_hit=item.cache_hit,
            )
            for item in instrumented_provider.call_evidence
        ]
        if synthetic_run is None:  # pragma: no cover - construction invariant
            raise RuntimeError("rightsizing run identity is unavailable")
        execution = ExperimentExecutionEvidence(
            run_id=experiment_run_id,
            campaign_id=campaign_id,
            provider_call_count=sum(
                item.provider_call_attempted and not item.cache_hit for item in calls
            ),
            measured_cost_usd=sum(item.measured_cost_usd or 0.0 for item in calls),
            estimated_cost_usd=sum(item.estimated_cost_usd or 0.0 for item in calls),
            calls=calls,
        )
        completed_report = report.model_copy(update={"execution": execution})
        completed_run = synthetic_run.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "failure_state": None,
                "completed_steps": [f"rightsizing:{body.node_id}"],
                "execution_history": _rightsizing_history(written_audits),
                "audit_refs": [record.audit_id for record in written_audits],
                "final_output": {
                    "provider_call_count": len(written_audits),
                    "verdict": report.verdict,
                    "rightsizing_experiment_report": completed_report.model_dump(mode="json"),
                },
            }
        )
        try:
            await bootstrap.run_repository.put(completed_run)
        except Exception:
            raise RuntimeError("rightsizing run completion persistence failed") from None
        return completed_report

    @app.get(
        "/econ/rightsizing/experiment/latest",
        response_model=ExperimentReport | None,
    )
    async def get_latest_rightsizing_experiment(request: Request) -> ExperimentReport | None:
        """Restore the latest completed measured result in the caller's deployment scope."""
        principal = await require_permission(request, Permission.METRICS_READ)
        bootstrap = getattr(request.app.state, "bootstrap", None)
        run_repository = getattr(bootstrap, "run_repository", None)
        if run_repository is None:
            raise HTTPException(status_code=503, detail="run repository not configured")
        deployment = getattr(bootstrap, "deployment", None)
        await require_deployment_scope(request, deployment)
        deployment_ref = getattr(deployment, "deployment_ref", None)
        for run in await run_repository.list_runs(
            deployment_ref,
            status=RunStatus.COMPLETED.value,
            limit=_RUN_READ_BOUND,
        ):
            if (
                not _completed(run)
                or getattr(run, "tenant_id", None) != principal.tenant_id
                or getattr(run, "workspace_id", None) != principal.workspace_id
            ):
                continue
            report = _stored_experiment_report(run)
            if report is not None:
                return report
        return None


def _rightsizing_history(records: list[NodeAuditRecord]) -> list[RunHistoryEntry]:
    return [
        RunHistoryEntry(
            node_id=record.node_id,
            status=record.status,
            audit_ref=record.audit_id,
            started_at=record.started_at,
            completed_at=record.completed_at,
            cost_usd=record.cost_usd,
            estimated_cost_usd=record.estimated_cost_usd,
            cost_measurement=record.cost_measurement,
        )
        for record in records
    ]


async def _write_rightsizing_call_audits(
    *,
    audit_repository: AuditRepository,
    calls: list[object],
    run_id: str,
    campaign_id: str,
    node_id: str,
    deployment: object,
    actor: object,
) -> list[NodeAuditRecord]:
    """Persist one signed typed record for each non-cache provider attempt."""
    written: list[NodeAuditRecord] = []
    for item in calls:
        if not item.provider_call_attempted or item.cache_hit:
            continue
        if item.cost_event_id is None:
            raise RuntimeError("rightsizing provider call lacks a cost event identity")
        now = datetime.now(UTC)
        measured = float(item.measured_cost_usd) if item.measured_cost_usd is not None else None
        estimated = float(item.estimated_cost_usd) if item.estimated_cost_usd is not None else None
        record = NodeAuditRecord(
            audit_id=f"rightsizing.call:{uuid4().hex}",
            run_id=run_id,
            node_id=f"rightsizing:{node_id}",
            graph_version_ref=getattr(deployment, "graph_version_ref", "service"),
            deployment_ref=getattr(deployment, "deployment_ref", "unknown"),
            tenant_id=getattr(deployment, "tenant_id", "default"),
            workspace_id=getattr(deployment, "workspace_id", None),
            campaign_id=campaign_id,
            status="completed",
            actor=actor,
            token_usage=(
                TokenUsage(
                    input_tokens=item.input_tokens or 0,
                    output_tokens=item.output_tokens or 0,
                    model_name=item.model_name,
                )
                if item.input_tokens is not None or item.output_tokens is not None
                else None
            ),
            cost_usd=measured,
            estimated_cost_usd=estimated,
            cost_measurement=item.cost_measurement,
            cost_event_id=item.cost_event_id,
            execution_metadata={
                "operation_id": item.operation_id,
                "cleanup_status": item.cleanup_status,
                "provider_call_attempted": True,
                "cache_hit": False,
            },
            started_at=now,
            completed_at=now,
        )
        stored = await audit_repository.write(record)
        if stored.record_signature is None:
            raise RuntimeError("rightsizing provider-call audit was not signed")
        written.append(stored)
    return written
