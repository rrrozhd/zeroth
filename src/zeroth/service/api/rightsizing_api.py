"""Model right-sizing REST API (ECON-RIGHTSIZE-01).

Exposes the design-time nudge the studio inspector shows under a node's model field:
cheaper, capability-compatible alternatives to the model the user picked. Pure lookup
over litellm's model DB — no Regulus, no network — so it works even when cost tracking
is not configured.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.econ.opportunities import SpendReport, spend_opportunities
from zeroth.core.econ.quality import read_quality_verdict
from zeroth.core.econ.rightsizing import RightsizingResult, describe, recommend
from zeroth.core.econ.rightsizing_experiment import (
    ExperimentReport,
    build_experiment_dataset,
    build_labeled_dataset,
    run_experiment,
)
from zeroth.governance.audit.models import AuditQuery
from zeroth.runtime.agents.provider import LiteLLMProviderAdapter
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


class ExperimentRequest(BaseModel):
    """Request body for POST /v1/econ/rightsizing/experiment (measured right-sizing).

    Replays this node's real audit-trail inputs through cheaper candidate models and scores
    equivalence to the incumbent. Runs real LLM calls, so the sample is capped small — the
    result is a *flagged* lead below ``min_cases``, not a *confirmed* switch.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
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
        deployment = getattr(bootstrap, "deployment", None)
        await require_deployment_scope(request, deployment)
        records = await bootstrap.audit_repository.list(
            AuditQuery(deployment_ref=getattr(deployment, "deployment_ref", None))
        )
        return spend_opportunities(records)

    @app.post("/econ/rightsizing/experiment", response_model=ExperimentReport)
    async def run_rightsizing_experiment(
        request: Request, body: ExperimentRequest
    ) -> ExperimentReport:
        """Replay this node's real inputs through cheaper models and measure equivalence.

        Every branch returns a 200 with an explanatory ``note`` rather than an error: an
        unpriced incumbent, no cheaper candidates, no run history, or a missing provider key
        are all *results* of the measurement, not failures of the endpoint.
        """
        await require_permission(request, Permission.WORKFLOW_READ)
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

        records = await bootstrap.audit_repository.list(
            AuditQuery(node_id=body.node_id, deployment_ref=deployment_ref)
        )
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
            runs = await run_repository.list_runs(deployment_ref, limit=500)
            expected_by_run: dict[str, str] = {}
            for run in runs:
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

        provider = LiteLLMProviderAdapter()
        return await run_experiment(
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
