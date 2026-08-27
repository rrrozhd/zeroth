from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.live_evaluation.config import CampaignConfig

ROOT = Path(__file__).parents[2]


def _config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": "evaluation-studio-v1",
        "tenant_id": "evaluation-studio-v1",
        "provider": "openai",
        "model": "openai/gpt-4o-mini",
        "embedding_model": "openai/text-embedding-3-small",
        "vector_backend": "chroma",
        "campaign_budget_usd": "10.00",
        "per_run_cap_usd": "0.25",
        "provider_secret_ref": "llm.openai",
        "artifact_root": "/tmp/zeroth-live-evaluation",
        "action_sink_root": "/tmp/zeroth-live-evaluation/action-sink",
    }
    value.update(overrides)
    return value


def test_campaign_profile_pins_the_safe_starting_boundary() -> None:
    config = CampaignConfig.model_validate(_config())

    assert config.campaign_budget_usd == Decimal("10.00")
    assert config.per_run_cap_usd == Decimal("0.25")
    assert config.vector_backend == "chroma"
    assert config.action_sink_root.is_relative_to(config.artifact_root)


def test_repository_campaign_profile_is_valid() -> None:
    payload = json.loads(
        (ROOT / "release/live_evaluation/campaign-v1.json").read_text(encoding="utf-8")
    )

    config = CampaignConfig.model_validate(payload)

    assert config.campaign_id == "evaluation-studio-v1"
    assert config.campaign_budget_usd == Decimal("10.00")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tenant_id", "default", "dedicated"),
        ("tenant_id", "production", "evaluation-"),
        ("campaign_budget_usd", "10.01", "campaign budget"),
        ("per_run_cap_usd", "0.251", "per-run cap"),
        ("provider_secret_ref", "OPENAI KEY", "logical secret"),
        ("action_sink_root", "/tmp/unscoped-sink", "artifact root"),
    ],
)
def test_campaign_profile_rejects_unsafe_scope(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CampaignConfig.model_validate(_config(**{field: value}))


def test_paid_resolution_requires_acknowledgement_and_credential() -> None:
    config = CampaignConfig.model_validate(_config())

    class Provider:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def resolve_secret(self, logical_name: str, *, tenant_id: str | None = None):
            assert logical_name == "llm.openai"
            assert tenant_id == "evaluation-studio-v1"
            return self.value

    with pytest.raises(ValueError, match="acknowledgement"):
        config.resolve_paid(Provider("not-a-real-key"), acknowledge_external_cost=False)
    with pytest.raises(ValueError, match="llm.openai"):
        config.resolve_paid(Provider(None), acknowledge_external_cost=True)

    resolved = config.resolve_paid(
        Provider("not-a-real-key"), acknowledge_external_cost=True
    )
    assert "not-a-real-key" not in resolved.model_dump_json()
