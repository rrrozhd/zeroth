from __future__ import annotations

import pytest

from tests.graph.test_models import build_graph
from zeroth.graph.diff import GraphDiff
from zeroth.graph.errors import GraphLifecycleError
from zeroth.graph.models import GraphStatus
from zeroth.graph.repository import GraphRepository
from zeroth.graph.storage import GRAPH_SCHEMA_VERSION


def test_graph_repository_round_trip_and_schema_version(sqlite_db) -> None:
    repository = GraphRepository(sqlite_db)
    graph = build_graph()

    saved = repository.save(graph)
    loaded = repository.get(graph.graph_id)

    assert saved == graph
    assert loaded == graph
    assert sqlite_db.fetch_schema_version("graphs") == GRAPH_SCHEMA_VERSION


def test_graph_repository_updates_status(sqlite_db) -> None:
    repository = GraphRepository(sqlite_db)
    graph = build_graph()
    repository.save(graph)

    updated = repository.update_status(graph.graph_id, GraphStatus.PUBLISHED)

    assert updated.status == GraphStatus.PUBLISHED
    assert repository.get(graph.graph_id).status == GraphStatus.PUBLISHED


def test_graph_repository_clone_published_to_new_draft_and_preserve_history(sqlite_db) -> None:
    repository = GraphRepository(sqlite_db)
    original = repository.create(build_graph())
    published = repository.publish(original.graph_id, original.version)

    cloned = repository.clone_published_to_draft(original.graph_id, original.version)

    assert published.status == GraphStatus.PUBLISHED
    assert cloned.status == GraphStatus.DRAFT
    assert cloned.version == 2
    assert cloned.graph_id == published.graph_id
    assert all(
        node.graph_version_ref == f"{cloned.graph_id}@{cloned.version}"
        for node in cloned.nodes
    )
    assert [graph.version for graph in repository.list_versions(original.graph_id)] == [1, 2]


def test_graph_repository_rejects_mutating_published_graph(sqlite_db) -> None:
    repository = GraphRepository(sqlite_db)
    graph = repository.create(build_graph())
    repository.publish(graph.graph_id, graph.version)

    published = repository.get(graph.graph_id, 1)
    assert published is not None

    with pytest.raises(GraphLifecycleError, match="immutable"):
        repository.save(published.model_copy(update={"name": "Mutated"}))

    with pytest.raises(GraphLifecycleError, match="cannot revert to draft"):
        repository.update_status(graph.graph_id, GraphStatus.DRAFT, version=1)


def test_graph_repository_diff_detects_semantic_changes(sqlite_db) -> None:
    repository = GraphRepository(sqlite_db)
    original = repository.create(build_graph())
    repository.publish(original.graph_id, original.version)
    cloned = repository.clone_published_to_draft(original.graph_id, original.version)

    modified_nodes = list(cloned.nodes)
    modified_nodes[0] = modified_nodes[0].model_copy(
        update={
            "input_contract_ref": "contract://input.v2",
            "policy_bindings": ["policy://safety", "policy://agent-updated"],
            "agent": modified_nodes[0].agent.model_copy(  # type: ignore[union-attr]
                update={"memory_refs": ["memory://run", "memory://shared"]}
            ),
        }
    )
    modified_nodes[1] = modified_nodes[1].model_copy(
        update={
            "executable_unit": modified_nodes[1].executable_unit.model_copy(  # type: ignore[union-attr]
                update={"manifest_ref": "eu://summarizer-v2"}
            )
        }
    )
    modified_edges = list(cloned.edges)
    modified_edges[0] = modified_edges[0].model_copy(
        update={
            "condition": modified_edges[0].condition.model_copy(  # type: ignore[union-attr]
                update={"expression": "payload.user.id is not None and approved"}
            )
        }
    )
    modified = cloned.model_copy(update={"nodes": modified_nodes, "edges": modified_edges})
    repository.save(modified)

    diff = repository.diff(original.graph_id, 1, 2)

    assert isinstance(diff, GraphDiff)
    assert not diff.is_empty()
    assert diff.node_changes
    assert diff.condition_changes
    assert diff.contract_changes
    assert diff.policy_changes
    assert diff.memory_connector_changes
    assert diff.executable_unit_binding_changes
