"""Tests for passive right-sizing opportunities (ECON-RIGHTSIZE-03).

Aggregation logic uses synthetic audit records; ``recommend`` runs against the real litellm
DB, so tests assert relationships (cheaper alternatives exist for gpt-4o, none for a made-up
model) rather than exact candidate counts.
"""

from __future__ import annotations

from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage, ToolCallRecord
from zeroth.econ.analytics.opportunities import spend_opportunities


def _rec(
    node_id: str,
    *,
    cost: float | None,
    estimated_cost: float | None = None,
    model: str = "gpt-4o",
    status: str = "completed",
    tools: bool = False,
) -> NodeAuditRecord:
    return NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id=f"{node_id}-{cost}-{status}-{tools}",
        run_id="r",
        node_id=node_id,
        graph_version_ref="g",
        deployment_ref="default",
        status=status,
        cost_usd=cost,
        estimated_cost_usd=estimated_cost,
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
            tenant_id="default",
            workspace_id=None,
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


def test_neutral_spend_is_not_projected_as_named_model_savings():
    report = spend_opportunities(
        [_rec("agent", cost=0.10), _rec("agent", cost=0.50, model="")]
    )

    assert report.total_cost_usd == 0.60
    [node] = report.nodes
    assert node.incumbent_model == "gpt-4o"
    assert node.runs == 1
    assert node.total_cost_usd == 0.10
    assert node.mean_cost_per_call_usd == 0.10
    assert node.projected_savings_usd == round(
        node.total_cost_usd * node.best_savings_pct / 100.0, 4
    )


def test_empty_audits_yield_guidance():
    report = spend_opportunities([])
    assert report.nodes == []
    assert "run some agent nodes" in report.note.lower()


def test_estimated_only_spend_surfaces_without_becoming_measured():
    report = spend_opportunities([_rec("agent", cost=None, estimated_cost=0.20)])

    assert report.total_cost_usd == 0.0
    assert report.total_estimated_cost_usd == 0.20
    [node] = report.nodes
    assert node.total_cost_usd == 0.0
    assert node.mean_cost_per_call_usd == 0.0
    assert node.total_estimated_cost_usd == 0.20
    assert node.mean_estimated_cost_per_call_usd == 0.20
    assert node.projected_savings_usd == 0.0
    assert node.projected_estimated_savings_usd
    assert node.projected_estimated_savings_usd > 0
    assert "estimated" in report.note.lower()


def test_mixed_spend_channels_do_not_cross_contaminate():
    report = spend_opportunities(
        [
            _rec("agent", cost=0.10),
            _rec("agent", cost=None, estimated_cost=0.40),
        ]
    )

    assert report.total_cost_usd == 0.10
    assert report.total_estimated_cost_usd == 0.40
    [node] = report.nodes
    assert node.total_cost_usd == 0.10
    assert node.total_estimated_cost_usd == 0.40
    assert node.mean_cost_per_call_usd == 0.05
    assert node.mean_estimated_cost_per_call_usd == 0.20


def test_estimated_spend_still_honors_replayable_run_filter():
    replayable = _rec("agent", cost=None, estimated_cost=0.20)
    control_probe = replayable.model_copy(
        update={"audit_id": "probe", "run_id": "provider-probe", "estimated_cost_usd": 9.0}
    )

    report = spend_opportunities(
        [replayable, control_probe], eligible_run_ids={replayable.run_id}
    )

    assert report.total_estimated_cost_usd == 0.20
    assert report.nodes[0].total_estimated_cost_usd == 0.20


def test_provider_lifecycle_and_runtime_audits_count_one_replayable_call():
    cost_event_id = "probe_shared"
    lifecycle = _rec(
        "zeroth-cap-opaque",
        cost=0.20,
        model="zeroth-impl-opaque",
    ).model_copy(
        update={
            "audit_id": f"audit_{cost_event_id}",
            "cost_event_id": cost_event_id,
            "cost_measurement": "estimated",
        }
    )
    runtime = _rec(
        "branch:0:subgraph:child:1:agent",
        cost=None,
        estimated_cost=0.20,
        model="openai/gpt-4o-mini",
    ).model_copy(
        update={
            "audit_id": "run:audit:2",
            "cost_event_id": cost_event_id,
            "cost_measurement": "estimated",
        }
    )

    branch_rollup = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="parent:branch:0:audit:1",
        run_id="r",
        node_id="subgraph",
        graph_version_ref="parent@1",
        deployment_ref="parent",
        status="completed",
        estimated_cost_usd=0.20,
        execution_metadata={"branch_id": "parent:branch:0"},
    )

    report = spend_opportunities(
        [lifecycle, runtime, branch_rollup], eligible_run_ids={"r"}
    )

    assert report.total_cost_usd == 0.0
    assert report.total_estimated_cost_usd == 0.20
    assert len(report.nodes) == 1
    assert report.nodes[0].node_id == "agent"
    assert report.nodes[0].source_deployment_ref == "default"
    assert report.nodes[0].runs == 1


def test_parallel_subgraph_repetitions_aggregate_under_authored_node_identity():
    first = _rec(
        "branch:0:subgraph:child:1:analyze",
        cost=None,
        estimated_cost=0.20,
        model="openai/gpt-4o-mini",
    ).model_copy(update={"audit_id": "branch-0", "cost_event_id": "event-0"})
    second = _rec(
        "branch:1:subgraph:child:1:analyze",
        cost=None,
        estimated_cost=0.30,
        model="openai/gpt-4o-mini",
    ).model_copy(update={"audit_id": "branch-1", "cost_event_id": "event-1"})

    report = spend_opportunities([first, second], eligible_run_ids={"r"})

    assert len(report.nodes) == 1
    assert report.nodes[0].node_id == "analyze"
    assert report.nodes[0].source_deployment_ref == "default"
    assert report.nodes[0].runs == 2
    assert report.nodes[0].total_estimated_cost_usd == 0.5


def test_equal_authored_ids_from_distinct_deployments_remain_separate():
    first = _rec(
        "branch:0:subgraph:child-a:1:analyze",
        cost=None,
        estimated_cost=0.20,
        model="openai/gpt-4o-mini",
    ).model_copy(
        update={
            "audit_id": "child-a",
            "cost_event_id": "event-a",
            "deployment_ref": "child-a",
        }
    )
    second = _rec(
        "branch:1:subgraph:child-b:1:analyze",
        cost=None,
        estimated_cost=0.30,
        model="openai/gpt-4o-mini",
    ).model_copy(
        update={
            "audit_id": "child-b",
            "cost_event_id": "event-b",
            "deployment_ref": "child-b",
        }
    )

    report = spend_opportunities([first, second], eligible_run_ids={"r"})

    assert len(report.nodes) == 2
    assert {(node.node_id, node.source_deployment_ref) for node in report.nodes} == {
        ("analyze", "child-a"),
        ("analyze", "child-b"),
    }
