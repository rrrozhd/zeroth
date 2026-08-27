"""Passive right-sizing opportunities — where to point the experiment (ECON-RIGHTSIZE-03).

Mode A and B are node-inspector surfaces: the user has to already be looking at a node to
learn it could be cheaper. Mode C is the dashboard half — it reads the whole audit trail,
attributes spend to each agent node, and ranks the nodes where a cheaper capable model
*exists* by how much they cost. It's the "start here" list that hands Mode B its targets.

Pure aggregation over ``NodeAuditRecord`` plus Mode A's ``recommend`` — no LLM calls, no
network. Honest about its own limits: ``projected_savings_usd`` is an *upper bound* (all
runs switched, equivalence unverified), and only nodes with tool-free runs *and* a cheaper
capable alternative are marked ``experiment_ready`` — the rest are surfaced but not oversold.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from zeroth.econ.analytics.identity import authored_node_id
from zeroth.econ.analytics.rightsizing import recommend
from zeroth.governance.audit.models import NodeAuditRecord

_SUCCESS_STATUSES = {"completed", "success", "succeeded"}


class NodeSpend(BaseModel):
    """Attributed spend and right-sizing potential for one node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    source_deployment_ref: str | None = None
    runs: int = 0
    total_cost_usd: float = 0.0
    mean_cost_per_call_usd: float = 0.0
    # Local model-price estimates are reported separately from measured dollars.
    total_estimated_cost_usd: float = 0.0
    mean_estimated_cost_per_call_usd: float = 0.0
    incumbent_model: str | None = None
    uses_tools: bool = False
    tool_free_runs: int = 0
    cheaper_alternatives: int = 0
    best_savings_pct: float | None = None
    # Upper bound: total observed spend × best candidate savings, IF every run switched and
    # the cheaper model proved equivalent (which only Mode B measures). A ceiling, not a promise.
    projected_savings_usd: float | None = None
    projected_estimated_savings_usd: float | None = None
    # Tool-free runs exist AND a cheaper capable model exists — ready for a measured experiment.
    experiment_ready: bool = False


class SpendReport(BaseModel):
    """Deployment-wide spend attribution, ranked by right-sizing opportunity."""

    model_config = ConfigDict(extra="forbid")

    total_cost_usd: float = 0.0
    total_estimated_cost_usd: float = 0.0
    nodes: list[NodeSpend] = Field(default_factory=list)
    note: str = ""


def _dominant_model(models: list[str]) -> str | None:
    """The most frequently-seen model name in a node's records (its incumbent)."""
    named = [m for m in models if m]
    if not named:
        return None
    return Counter(named).most_common(1)[0][0]


def _canonical_priced_records(records: Sequence[NodeAuditRecord]) -> list[NodeAuditRecord]:
    """Return one replayable audit row per durable provider-call identity.

    Instrumentation writes a lifecycle row named ``audit_<cost_event_id>`` and
    the runtime writes the workflow-node row. Both prove the same call. The
    runtime row is authoritative for node/model attribution and its measurement
    channel, while the lifecycle row remains in the signed evidence bundle.
    """
    unkeyed: list[NodeAuditRecord] = []
    keyed: dict[str, list[NodeAuditRecord]] = {}
    for record in records:
        if record.cost_event_id is None:
            # A composed parent persists the child subgraph's cost as a branch
            # rollup for timeline display. The child call itself is separately
            # durable and carries token/model + cost-event identity; counting
            # this un-attributed rollup would double the deployment total.
            if (
                record.token_usage is None
                and record.execution_metadata.get("branch_id") is not None
                and (
                    float(record.cost_usd or 0.0) > 0.0
                    or float(record.estimated_cost_usd or 0.0) > 0.0
                )
            ):
                continue
            unkeyed.append(record)
            continue
        keyed.setdefault(record.cost_event_id, []).append(record)

    canonical = list(unkeyed)
    for cost_event_id, duplicates in keyed.items():
        runtime_rows = [
            record
            for record in duplicates
            if record.audit_id != f"audit_{cost_event_id}"
        ]
        candidates = runtime_rows or duplicates
        canonical.append(
            max(
                candidates,
                key=lambda record: (
                    record.token_usage is not None,
                    record.estimated_cost_usd is not None,
                    record.cost_usd is not None,
                ),
            )
        )
    return canonical


def spend_opportunities(
    audits: Sequence[NodeAuditRecord],
    *,
    min_savings_pct: float = 20.0,
    limit: int = 20,
    eligible_run_ids: set[str] | None = None,
) -> SpendReport:
    """Attribute spend per node and rank nodes by right-sizing opportunity.

    Considers only nodes that actually spent money (LLM/agent nodes). For each, finds the
    dominant model, whether it used tools, and — via Mode A's ``recommend`` — whether a
    cheaper capable model exists. Nodes are ranked by total spend (biggest bill first), so
    the top of the list is where a swap saves the most. ``recommend`` is memoized per
    ``(model, uses_tools)`` so a deployment with many nodes on one model costs one lookup.
    """
    eligible_records: list[NodeAuditRecord] = []
    for record in audits:
        # The dashboard must only promote traffic that the measured experiment can
        # actually replay. Provider verification, embedding probes, and other
        # control-plane operations can emit model-bearing audit records, but they do
        # not have a persisted workflow Run and therefore cannot be opened in Studio.
        if eligible_run_ids is not None and record.run_id not in eligible_run_ids:
            continue
        eligible_records.append(record)

    by_node: dict[tuple[str, str], list[NodeAuditRecord]] = {}
    for record in _canonical_priced_records(eligible_records):
        key = (record.deployment_ref, authored_node_id(record.node_id))
        by_node.setdefault(key, []).append(record)

    reco_cache: dict[tuple[str, bool], list] = {}

    def _candidates(model: str, uses_tools: bool) -> list:
        key = (model, uses_tools)
        if key not in reco_cache:
            reco_cache[key] = recommend(
                model, needs_tools=uses_tools, min_savings_pct=min_savings_pct, limit=6
            ).candidates
        return reco_cache[key]

    nodes: list[NodeSpend] = []
    total_cost = 0.0
    total_estimated_cost = 0.0
    for (source_deployment_ref, node_id), records in by_node.items():
        total_cost += sum(r.cost_usd or 0.0 for r in records)
        total_estimated_cost += sum(r.estimated_cost_usd or 0.0 for r in records)
        attributed_records = [
            record
            for record in records
            if record.token_usage is not None and record.token_usage.model_name
        ]
        node_cost = sum(r.cost_usd or 0.0 for r in attributed_records)
        node_estimated_cost = sum(r.estimated_cost_usd or 0.0 for r in attributed_records)
        if node_cost <= 0 and node_estimated_cost <= 0:
            continue

        models = [r.token_usage.model_name for r in attributed_records if r.token_usage]
        incumbent = _dominant_model(models)
        uses_tools = any(r.tool_calls for r in attributed_records)
        tool_free_runs = sum(
            1
            for r in attributed_records
            if r.status in _SUCCESS_STATUSES and not r.tool_calls
        )

        candidates = _candidates(incumbent, uses_tools) if incumbent else []
        best_savings = max((c.savings_pct for c in candidates), default=None)
        projected = round(node_cost * best_savings / 100.0, 4) if best_savings is not None else None
        projected_estimated = (
            round(node_estimated_cost * best_savings / 100.0, 4)
            if best_savings is not None
            else None
        )

        nodes.append(
            NodeSpend(
                node_id=node_id,
                source_deployment_ref=source_deployment_ref,
                runs=len(attributed_records),
                total_cost_usd=round(node_cost, 6),
                mean_cost_per_call_usd=round(node_cost / len(attributed_records), 6),
                total_estimated_cost_usd=round(node_estimated_cost, 6),
                mean_estimated_cost_per_call_usd=round(
                    node_estimated_cost / len(attributed_records), 6
                ),
                incumbent_model=incumbent,
                uses_tools=uses_tools,
                tool_free_runs=tool_free_runs,
                cheaper_alternatives=len(candidates),
                best_savings_pct=best_savings,
                projected_savings_usd=projected,
                projected_estimated_savings_usd=projected_estimated,
                experiment_ready=tool_free_runs > 0 and len(candidates) > 0,
            )
        )

    nodes.sort(
        key=lambda n: max(n.total_cost_usd, n.total_estimated_cost_usd),
        reverse=True,
    )
    nodes = nodes[:limit]

    ready = sum(1 for n in nodes if n.experiment_ready)
    estimated_only = total_cost == 0 and total_estimated_cost > 0
    if not nodes and eligible_run_ids is not None:
        note = (
            "No replayable workflow-agent spend is available yet. Control-plane probes are "
            "excluded because they cannot be opened or measured in Studio."
        )
    elif not nodes:
        note = "No model spend attributed yet — run some agent nodes to surface opportunities."
    elif estimated_only:
        note = (
            "Model spend is available only as locally estimated prices, not measured provider "
            "dollars. Use the ranked nodes to choose an equivalence experiment."
        )
    elif ready:
        note = (
            f"{ready} node(s) have a cheaper capable model and tool-free traffic to test on. "
            "Open one in Studio and run 'Measure equivalence' to see if a swap holds up."
        )
    else:
        note = (
            "Model spend is attributed, but no node has a materially cheaper capable alternative."
        )

    return SpendReport(
        total_cost_usd=round(total_cost, 6),
        total_estimated_cost_usd=round(total_estimated_cost, 6),
        nodes=nodes,
        note=note,
    )
