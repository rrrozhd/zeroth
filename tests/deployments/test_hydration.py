"""Deployed graphs always hydrate through their immutable engine-mode pin."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tests.graph.test_models import build_graph
from zeroth.contracts.graph import ExecutionSettings
from zeroth.contracts.graph.serialization import hydrate_deployed_graph, serialize_graph
from zeroth.contracts.graph.warnings import LegacyEngineDeprecationWarning
from zeroth.service.deployments import (
    Deployment,
    DeploymentEngineMode,
)


@pytest.mark.parametrize(
    ("serialized_flag", "engine_mode", "effective_flag"),
    [
        (None, DeploymentEngineMode.LEGACY, False),
        (True, DeploymentEngineMode.LEGACY, False),
        (False, DeploymentEngineMode.TOKEN, True),
    ],
)
def test_hydration_uses_deployment_pin_not_serialized_graph_setting(
    serialized_flag: bool | None,
    engine_mode: DeploymentEngineMode,
    effective_flag: bool,
) -> None:
    settings = (
        ExecutionSettings()
        if serialized_flag is None
        else ExecutionSettings.model_construct(sequential_join_enabled=serialized_flag)
    )
    graph = build_graph().model_copy(update={"execution_settings": settings})
    deployment = Deployment(
        deployment_id="deployment-1",
        deployment_ref="service-1",
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}@{graph.version}",
        serialized_graph=serialize_graph(graph),
        engine_mode=engine_mode,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LegacyEngineDeprecationWarning)
        hydrated = hydrate_deployed_graph(deployment)

    assert hydrated.execution_settings.sequential_join_enabled is effective_flag
    assert "sequential_join_enabled" in hydrated.execution_settings.model_fields_set


def test_every_deployment_hydration_call_site_uses_shared_helper() -> None:
    root = Path(__file__).parents[2] / "src" / "zeroth"
    call_sites = [
        root / "service" / "bootstrap" / "factory.py",
        root / "runtime" / "subgraphs" / "resolver.py",
    ]

    for path in call_sites:
        source = path.read_text()
        assert "hydrate_deployed_graph(" in source
        assert "deserialize_graph(deployment.serialized_graph)" not in source
