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

from zeroth.core.econ.rightsizing import recommend
from zeroth.governance.audit.models import NodeAuditRecord

_SUCCESS_STATUSES = {"completed", "success", "succeeded"}


class NodeSpend(BaseModel):
    """Attributed spend and right-sizing potential for one node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    runs: int = 0
    total_cost_usd: float = 0.0
    mean_cost_per_call_usd: float = 0.0
    incumbent_model: str | None = None
    uses_tools: bool = False
    tool_free_runs: int = 0
    cheaper_alternatives: int = 0
    best_savings_pct: float | None = None
    # Upper bound: total observed spend × best candidate savings, IF every run switched and
    # the cheaper model proved equivalent (which only Mode B measures). A ceiling, not a promise.
    projected_savings_usd: float | None = None
    # Tool-free runs exist AND a cheaper capable model exists — ready for a measured experiment.
    experiment_ready: bool = False


class SpendReport(BaseModel):
    """Deployment-wide spend attribution, ranked by right-sizing opportunity."""

    model_config = ConfigDict(extra="forbid")

    total_cost_usd: float = 0.0
    nodes: list[NodeSpend] = Field(default_factory=list)
    note: str = ""


def _dominant_model(models: list[str]) -> str | None:
    """The most frequently-seen model name in a node's records (its incumbent)."""
    named = [m for m in models if m]
    if not named:
        return None
    return Counter(named).most_common(1)[0][0]


def spend_opportunities(
    audits: Sequence[NodeAuditRecord],
    *,
    min_savings_pct: float = 20.0,
    limit: int = 20,
) -> SpendReport:
    """Attribute spend per node and rank nodes by right-sizing opportunity.

    Considers only nodes that actually spent money (LLM/agent nodes). For each, finds the
    dominant model, whether it used tools, and — via Mode A's ``recommend`` — whether a
    cheaper capable model exists. Nodes are ranked by total spend (biggest bill first), so
    the top of the list is where a swap saves the most. ``recommend`` is memoized per
    ``(model, uses_tools)`` so a deployment with many nodes on one model costs one lookup.
    """
    by_node: dict[str, list[NodeAuditRecord]] = {}
    for record in audits:
        by_node.setdefault(record.node_id, []).append(record)

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
    for node_id, records in by_node.items():
        node_cost = sum(r.cost_usd or 0.0 for r in records)
        total_cost += node_cost
        if node_cost <= 0:  # code/retrieval/approval nodes don't spend on models
            continue

        models = [r.token_usage.model_name for r in records if r.token_usage is not None]
        incumbent = _dominant_model(models)
        uses_tools = any(r.tool_calls for r in records)
        tool_free_runs = sum(
            1 for r in records if r.status in _SUCCESS_STATUSES and not r.tool_calls
        )

        candidates = _candidates(incumbent, uses_tools) if incumbent else []
        best_savings = max((c.savings_pct for c in candidates), default=None)
        projected = round(node_cost * best_savings / 100.0, 4) if best_savings is not None else None

        nodes.append(
            NodeSpend(
                node_id=node_id,
                runs=len(records),
                total_cost_usd=round(node_cost, 6),
                mean_cost_per_call_usd=round(node_cost / len(records), 6),
                incumbent_model=incumbent,
                uses_tools=uses_tools,
                tool_free_runs=tool_free_runs,
                cheaper_alternatives=len(candidates),
                best_savings_pct=best_savings,
                projected_savings_usd=projected,
                experiment_ready=tool_free_runs > 0 and len(candidates) > 0,
            )
        )

    nodes.sort(key=lambda n: n.total_cost_usd, reverse=True)
    nodes = nodes[:limit]

    ready = sum(1 for n in nodes if n.experiment_ready)
    if not nodes:
        note = "No model spend attributed yet — run some agent nodes to surface opportunities."
    elif ready:
        note = (
            f"{ready} node(s) have a cheaper capable model and tool-free traffic to test on. "
            "Open one in Studio and run 'Measure equivalence' to see if a swap holds up."
        )
    else:
        note = (
            "Model spend is attributed, but no node has a materially cheaper capable alternative."
        )

    return SpendReport(total_cost_usd=round(total_cost, 6), nodes=nodes, note=note)
