"""Tests for model right-sizing (ECON-RIGHTSIZE-01).

Two layers: structural assertions against litellm's real model DB (robust to price
drift — we assert *relationships*, not exact numbers), and a monkeypatched DB for
deterministic capability-gate and ranking behavior.
"""

from __future__ import annotations

import litellm
import pytest

from zeroth.core.econ.rightsizing import ModelOption, RightsizingResult, recommend

# --- A tiny synthetic model DB for deterministic gate/ranking tests. ------------
# Prices are per-token; kept obviously fake so nobody mistakes them for real ones.
_FAKE_DB = {
    "premium": {  # the incumbent
        "mode": "chat",
        "input_cost_per_token": 10e-6,
        "output_cost_per_token": 30e-6,
        "litellm_provider": "acme",
        "supports_function_calling": True,
        "supports_vision": True,
        "max_input_tokens": 200_000,
    },
    "acme-mini": {  # same provider, much cheaper, full capability
        "mode": "chat",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 3e-6,
        "litellm_provider": "acme",
        "supports_function_calling": True,
        "supports_vision": True,
        "max_input_tokens": 128_000,
    },
    "acme-mini-2025-08-07": {  # dated snapshot of acme-mini — must collapse into it
        "mode": "chat",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 3e-6,
        "litellm_provider": "acme",
        "supports_function_calling": True,
        "supports_vision": True,
        "max_input_tokens": 128_000,
    },
    "rival-cheap": {  # cross provider, cheaper, but NO tool calling
        "mode": "chat",
        "input_cost_per_token": 0.5e-6,
        "output_cost_per_token": 1.5e-6,
        "litellm_provider": "globex",
        "supports_function_calling": False,
        "supports_vision": False,
        "max_input_tokens": 32_000,
    },
    "rival-tools": {  # cross provider, cheaper, tool calling but no vision
        "mode": "chat",
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 6e-6,
        "litellm_provider": "globex",
        "supports_function_calling": True,
        "supports_vision": False,
        "max_input_tokens": 64_000,
    },
    "barely-cheaper": {  # only ~5% cheaper — below the default savings floor
        "mode": "chat",
        "input_cost_per_token": 9.5e-6,
        "output_cost_per_token": 28.5e-6,
        "litellm_provider": "globex",
        "supports_function_calling": True,
        "supports_vision": True,
    },
    "ft:acme-mini-2025-08-07": {  # fine-tune pricing template — never a selectable model
        "mode": "chat",
        "input_cost_per_token": 0.5e-6,
        "output_cost_per_token": 1.5e-6,
        "litellm_provider": "acme",
        "supports_function_calling": True,
    },
    "an-embedding": {  # wrong mode — must never be recommended as a chat swap
        "mode": "embedding",
        "input_cost_per_token": 0.01e-6,
        "output_cost_per_token": 0.0,
        "litellm_provider": "acme",
    },
    "sample_spec": {"mode": "chat"},  # litellm's placeholder row — must be skipped
}


@pytest.fixture
def fake_db(monkeypatch):
    """Point litellm at the synthetic DB and resolve the incumbent from it."""
    monkeypatch.setattr(litellm, "model_cost", _FAKE_DB, raising=False)

    def fake_get_model_info(model):
        info = _FAKE_DB.get(model)
        if info is None:
            raise Exception(f"unknown model {model}")
        return dict(info)

    monkeypatch.setattr(litellm, "get_model_info", fake_get_model_info)


# --- Deterministic behavior on the synthetic DB. --------------------------------


def test_recommends_cheaper_capability_compatible_models(fake_db):
    result = recommend("premium")
    assert result.incumbent_known is True
    assert result.incumbent_provider == "acme"
    models = [c.model for c in result.candidates]
    # acme-mini (same provider) and the two globex options clear the 20% floor;
    # barely-cheaper (5%) does not; the embedding and the incumbent are excluded.
    assert "acme-mini" in models
    assert "barely-cheaper" not in models
    assert "an-embedding" not in models
    assert "premium" not in models
    # "ft:…" fine-tune pricing rows are never selectable models.
    assert not any(m.startswith("ft:") or m == "acme-mini-2025-08-07" for m in models)


def test_dated_snapshots_collapse_into_undated_alias(fake_db):
    result = recommend("premium")
    models = [c.model for c in result.candidates]
    assert "acme-mini" in models
    assert "acme-mini-2025-08-07" not in models
    # exactly one row for the family, not one per snapshot
    assert models.count("acme-mini") == 1


def test_same_provider_ranked_first(fake_db):
    result = recommend("premium")
    # acme-mini shares the incumbent's provider, so it leads despite globex being cheaper.
    assert result.candidates[0].model == "acme-mini"
    assert result.candidates[0].same_provider is True


def test_needs_tools_gate_excludes_non_tool_models(fake_db):
    result = recommend("premium", needs_tools=True)
    models = {c.model for c in result.candidates}
    assert "rival-cheap" not in models  # no function calling
    assert "acme-mini" in models
    assert "rival-tools" in models
    assert all(c.supports_tools for c in result.candidates)


def test_needs_vision_gate_excludes_non_vision_models(fake_db):
    result = recommend("premium", needs_vision=True)
    models = {c.model for c in result.candidates}
    assert "rival-tools" not in models  # tools yes, vision no
    assert "rival-cheap" not in models
    assert "acme-mini" in models  # full capability


def test_savings_floor_is_respected(fake_db):
    # Drop the floor and barely-cheaper (5%) becomes eligible.
    result = recommend("premium", min_savings_pct=1.0)
    assert "barely-cheaper" in {c.model for c in result.candidates}


def test_limit_caps_results(fake_db):
    result = recommend("premium", limit=1)
    assert len(result.candidates) == 1


def test_savings_pct_and_ref_are_computed(fake_db):
    result = recommend("premium")
    mini = next(c for c in result.candidates if c.model == "acme-mini")
    # incumbent blended = (3*10 + 30)/4 = 15/Mtok; mini = (3*1+3)/4 = 1.5 → 90% cheaper.
    assert mini.savings_pct == pytest.approx(90.0, abs=0.5)
    assert mini.ref == "acme/acme-mini"


def test_unknown_incumbent_degrades_gracefully(fake_db):
    result = recommend("gpt-5.5-imaginary")
    assert result.incumbent_known is False
    assert result.candidates == []
    assert "newer than" in result.note or "No pricing" in result.note


def test_free_incumbent_has_nothing_cheaper(monkeypatch):
    db = {"freebie": {"mode": "chat", "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}}
    monkeypatch.setattr(litellm, "model_cost", db, raising=False)
    monkeypatch.setattr(litellm, "get_model_info", lambda m: dict(db[m]))
    result = recommend("freebie")
    assert result.incumbent_known is True
    assert result.candidates == []


# --- Structural assertions against the real litellm DB. -------------------------


def test_real_db_gpt4o_has_cheaper_alternatives():
    """gpt-4o is a real, priced model — right-sizing must find cheaper chat models."""
    result = recommend("gpt-4o", limit=8)
    assert result.incumbent_known is True
    assert result.incumbent_blended_per_mtok_usd and result.incumbent_blended_per_mtok_usd > 0
    assert result.candidates, "expected at least one cheaper alternative to gpt-4o"
    # Every candidate is genuinely cheaper and carries a positive savings figure.
    for c in result.candidates:
        assert isinstance(c, ModelOption)
        assert c.blended_per_mtok_usd < result.incumbent_blended_per_mtok_usd
        assert c.savings_pct > 0
    # Sorted same-provider-first, then ascending blended price within each group.
    keys = [(not c.same_provider, c.blended_per_mtok_usd) for c in result.candidates]
    assert keys == sorted(keys)


def test_real_db_result_is_serializable():
    """The result must round-trip to JSON — it's a FastAPI response_model."""
    result = recommend("gpt-4o", limit=3)
    dumped = RightsizingResult.model_validate_json(result.model_dump_json())
    assert dumped.incumbent == "gpt-4o"
