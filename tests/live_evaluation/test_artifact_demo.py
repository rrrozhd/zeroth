"""Artifact demo topology is bounded and publishes idempotently."""

from __future__ import annotations

import pytest

from release.live_evaluation.artifact_demo import (
    ARTIFACT_DEMO_DEPLOYMENT_REF,
    ARTIFACT_DEMO_GRAPH_ID,
    build_artifact_demo_graph,
    seed_artifact_demo,
)


def test_artifact_demo_is_one_bounded_manifest_node() -> None:
    graph = build_artifact_demo_graph(tenant_id="evaluation-artifact-test")
    assert graph.graph_id == ARTIFACT_DEMO_GRAPH_ID
    assert graph.entry_step == "emit-artifact"
    assert len(graph.nodes) == 1
    assert graph.version == 2
    assert graph.execution_settings.max_total_steps == 2
    assert graph.metadata["external_calls"] is False


@pytest.mark.asyncio
async def test_seed_artifact_demo_is_idempotent(sqlite_db) -> None:
    first = await seed_artifact_demo(sqlite_db, tenant_id="evaluation-artifact-test")
    second = await seed_artifact_demo(sqlite_db, tenant_id="evaluation-artifact-test")
    assert first.deployment_ref == ARTIFACT_DEMO_DEPLOYMENT_REF
    assert second.version == first.version
    assert second.graph_id == ARTIFACT_DEMO_GRAPH_ID
