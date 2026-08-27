"""Provider fault wrapping also supports provider-free deployments."""

from __future__ import annotations

from release.live_evaluation.service import _with_faulting_providers


def test_provider_free_deployment_has_empty_runner_inventory() -> None:
    assert _with_faulting_providers(None, object()) == {}
