"""Tests for passive right-sizing opportunities (ECON-RIGHTSIZE-03).

Aggregation logic uses synthetic audit records; ``recommend`` runs against the real litellm
DB, so tests assert relationships (cheaper alternatives exist for gpt-4o, none for a made-up
model) rather than exact candidate counts.
"""

from __future__ import annotations

from zeroth.core.audit.models import NodeAuditRecord, TokenUsage, ToolCallRecord
from zeroth.core.econ.opportunities import spend_opportunities


def _rec(
    node_id: str,
    *,
    cost: float,
    model: str = "gpt-4o",
    status: str = "completed",
    tools: bool = False,
) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=f"{node_id}-{cost}-{status}-{tools}",
        run_id="r",
        node_id=node_id,
        graph_version_ref="g",
        deployment_ref="default",
        status=status,
        cost_usd=cost,
        token_usage=TokenUsage(input_tokens=1000, output_tokens=200, model_name=model),
        tool_calls=[ToolCallRecord(tool_ref="t", alias="t")] if tools else [],
    )


def test_aggregates_and_ranks_by_spend():
    audits = [
        _rec("cheap_node", cost=0.01),
        _rec("cheap_node", cost=0.01),
        _rec("pricey_node", cost=0.50),
    ]
    report = spend_opportunities(audits)
    assert report.total_cost_usd == 0.52
    # Highest spend first.
    assert report.nodes[0].node_id == "pricey_node"
    assert report.nodes[1].node_id == "cheap_node"
    cheap = report.nodes[1]
    assert cheap.runs == 2
    assert cheap.total_cost_usd == 0.02
    assert cheap.mean_cost_per_call_usd == 0.01


def test_zero_cost_nodes_excluded():
    audits = [
        _rec("agent", cost=0.10),
        # a code/retrieval node: cost 0, no model
        NodeAuditRecord(
            audit_id="code-1",
            run_id="r",
            node_id="transform",
            graph_version_ref="g",
            deployment_ref="default",
            status="completed",
            cost_usd=0.0,
        ),
    ]
    report = spend_opportunities(audits)
    assert [n.node_id for n in report.nodes] == ["agent"]
    # But the code node's (zero) cost still counts toward the deployment total.
    assert report.total_cost_usd == 0.10


def test_known_model_node_is_experiment_ready():
    audits = [_rec("agent", cost=0.20), _rec("agent", cost=0.20)]
    report = spend_opportunities(audits)
    node = report.nodes[0]
    assert node.incumbent_model == "gpt-4o"
    assert node.uses_tools is False
    assert node.tool_free_runs == 2
    assert node.cheaper_alternatives > 0  # gpt-4o has cheaper capable models
    assert node.best_savings_pct and node.best_savings_pct > 0
    assert node.projected_savings_usd and node.projected_savings_usd > 0
    assert node.experiment_ready is True


def test_tool_using_node_is_not_experiment_ready_in_mvp():
    # Cheaper tool-capable models exist, but no tool-free traffic to replay → not ready.
    audits = [_rec("agent", cost=0.30, tools=True)]
    report = spend_opportunities(audits)
    node = report.nodes[0]
    assert node.uses_tools is True
    assert node.tool_free_runs == 0
    assert node.experiment_ready is False


def test_unpriced_model_has_no_alternatives():
    audits = [_rec("agent", cost=0.10, model="totally-made-up-model")]
    report = spend_opportunities(audits)
    node = report.nodes[0]
    assert node.cheaper_alternatives == 0
    assert node.experiment_ready is False


def test_empty_audits_yield_guidance():
    report = spend_opportunities([])
    assert report.nodes == []
    assert "run some agent nodes" in report.note.lower()
