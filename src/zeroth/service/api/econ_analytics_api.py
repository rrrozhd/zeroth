"""Econ analytics REST API -- unit economics + waste over the run trail (ECON-UNIT/WASTE).

Sibling to ``rightsizing_api``: read-only aggregation over the audit and run trail, no
LLM calls and no Regulus proxying (unlike ``cost_api``, which queries Regulus for
aggregate KPIs). Joins authoritative per-run outcomes from the run repository with
per-run spend from the audit repository into cost-per-successful-outcome, the failure
tax, and a structural-waste rollup -- the numbers that say whether an app's unit
economics are viable and where its spend leaks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.audit.models import AuditQuery, NodeAuditRecord
from zeroth.core.econ.quality import RunQualityVerdict
from zeroth.core.econ.unit_economics import UnitEconomicsReport, unit_economics
from zeroth.core.econ.waste import WasteRollup, waste_rollup
from zeroth.core.runs.models import Run
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)


class QualityVerdictRequest(BaseModel):
    """Request body for POST /econ/quality-verdict."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    verdict: Literal["good", "bad", "unknown"]
    source: str = Field(min_length=1, description="Who/what judged it, e.g. 'human:alice'")
    score: float | None = None
    rubric_id: str | None = None
    detail: str = ""
    # Optional: the reviewer's *correct* answer, which makes this run a labeled case for
    # correctness-graded right-sizing (grade a candidate model against this, not the incumbent).
    expected_output: str | None = None


async def _windowed_runs_and_audits(
    request: Request, window: int
) -> tuple[list[Run], list[NodeAuditRecord]]:
    """Fetch the last ``window`` deployment runs plus their in-window audit records.

    Cost is attributed only to runs in the window so the dollar figures stay internally
    consistent with the run population they are divided by. Raises 503 when the
    repositories or deployment are unconfigured. Callers check their own permission first.
    """
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if (
        bootstrap is None
        or getattr(bootstrap, "audit_repository", None) is None
        or getattr(bootstrap, "run_repository", None) is None
    ):
        raise HTTPException(status_code=503, detail="run/audit repository not configured")
    deployment = getattr(bootstrap, "deployment", None)
    await require_deployment_scope(request, deployment)
    deployment_ref = getattr(deployment, "deployment_ref", None)
    if deployment_ref is None:
        raise HTTPException(status_code=503, detail="deployment not configured")

    runs = await bootstrap.run_repository.list_runs(deployment_ref, limit=window)
    run_ids = {run.run_id for run in runs}
    records = await bootstrap.audit_repository.list(AuditQuery(deployment_ref=deployment_ref))
    scoped = [r for r in records if r.run_id in run_ids]
    return runs, scoped


def register_econ_analytics_routes(app: FastAPI | APIRouter) -> None:
    """Register econ analytics routes on the FastAPI app."""

    @app.get("/econ/unit-economics", response_model=UnitEconomicsReport)
    async def get_unit_economics(
        request: Request,
        window: int = Query(default=200, ge=1, le=1000),
    ) -> UnitEconomicsReport:
        """Cost per successful outcome and the failure tax over the last ``window`` runs.

        Read-only aggregation over the run + audit trail — no LLM calls, no Regulus. Every
        state is a 200: no runs, no attributed cost, or no successes yet are all *results*
        carried in the report's ``note``, never errors. Returns 503 only when the
        repositories themselves are unavailable.
        """
        await require_permission(request, Permission.METRICS_READ)
        runs, scoped = await _windowed_runs_and_audits(request, window)
        return unit_economics(runs, scoped)

    @app.get("/econ/waste", response_model=WasteRollup)
    async def get_waste(
        request: Request,
        window: int = Query(default=200, ge=1, le=1000),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> WasteRollup:
        """Deployment-wide structural-waste rollup over the last ``window`` runs.

        Runs ``analyze_run`` per top-level run and sums confirmed (paid-for-failed) vs
        flagged (loops/retries) waste — which never share a dollar. Read-only, no LLM, no
        Regulus. Every data state is a 200 carried in ``note``; ``limit`` caps the returned
        top findings. Returns 503 only when the repositories are unavailable.
        """
        await require_permission(request, Permission.METRICS_READ)
        runs, scoped = await _windowed_runs_and_audits(request, window)
        return waste_rollup(runs, scoped, top_n=limit)

    @app.post("/econ/quality-verdict", response_model=RunQualityVerdict)
    async def attach_quality_verdict(
        request: Request, body: QualityVerdictRequest
    ) -> RunQualityVerdict:
        """Attach an external good/bad verdict to a terminal run for quality-aware economics.

        The runtime can't know whether an answer was *good*, so this is the honest way to
        feed that signal in: a human reviewer or downstream check records a verdict, and
        unit economics then reports cost per good outcome over the labeled subset.

        Annotation, not evidence — it merges under ``run.metadata['quality_verdict']`` and is
        deliberately off the audit hash-chain. Admin-only (``METRICS_ADMIN``). 404 for an
        unknown or out-of-deployment run; 409 for a non-terminal run (no outcome to judge yet).
        """
        await require_permission(request, Permission.METRICS_ADMIN)
        bootstrap = getattr(request.app.state, "bootstrap", None)
        if bootstrap is None or getattr(bootstrap, "run_repository", None) is None:
            raise HTTPException(status_code=503, detail="run repository not configured")
        deployment = getattr(bootstrap, "deployment", None)
        await require_deployment_scope(request, deployment)
        deployment_ref = getattr(deployment, "deployment_ref", None)

        run = await bootstrap.run_repository.get(body.run_id)
        if run is None or (deployment_ref is not None and run.deployment_ref != deployment_ref):
            raise HTTPException(status_code=404, detail="run not found")
        status = getattr(run.status, "value", run.status)
        if status not in {"COMPLETED", "FAILED"}:
            raise HTTPException(status_code=409, detail="run is not terminal yet")

        verdict = RunQualityVerdict(
            verdict=body.verdict,
            score=body.score,
            source=body.source,
            rubric_id=body.rubric_id,
            detail=body.detail,
            expected_output=body.expected_output,
            attached_at=datetime.now(UTC),
        )
        # Merge under a single key so any other run.metadata is preserved (never clobbered).
        run.metadata = {**(run.metadata or {}), "quality_verdict": verdict.model_dump(mode="json")}
        await bootstrap.run_repository.put(run)
        return verdict
