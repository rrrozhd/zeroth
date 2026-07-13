"""Model right-sizing — cheaper capability-compatible alternatives (ECON-RIGHTSIZE-01).

The design-time half of the "model right-sizing" shape ``waste.py`` defers: given the
model a user picked for a node, surface *cheaper models that can still do the job*.

Two claims live in "a cheaper model that's just as good": **cheaper** is free (litellm
prices every model) and **just as good** is the whole problem. This module answers only
the honest, verifiable half — capability compatibility and price — and is explicit that
it does not measure quality. It returns *candidates to test*, never a verdict that a swap
is safe. Measuring "just as good" (replaying real traffic and scoring equivalence to the
incumbent) is the runtime half, and lives elsewhere.

Everything here is **derived from litellm's model DB** (``litellm.model_cost`` /
``get_model_info``): pricing *and* capability flags (``supports_function_calling``,
``supports_vision``, ``max_input_tokens``, ``mode``) for ~3k models. Nothing is
hand-maintained, so the catalog cannot rot the way a hardcoded price table would.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

# Trailing dated-snapshot / rolling-alias suffixes: "-2025-08-07", "-20250807", "-latest".
# Stripped only to *group* a model with its dated snapshots so the picker shows one row
# (the undated alias) instead of three near-identical ones.
_DATE_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8}|latest)$")

# A blended $/token weights input vs output by a typical agent-call ratio, because
# ranking on input-price or output-price alone flips the order for models with lopsided
# pricing. 3:1 (three input tokens per output token) is a reasonable default for the
# instruction-following / tool-using calls this platform runs; it is surfaced to the
# caller as an explicit assumption rather than hidden.
_INPUT_WEIGHT = 3
_OUTPUT_WEIGHT = 1
_BLEND_ASSUMPTION = "blended price assumes a 3:1 input:output token ratio"


class ModelOption(BaseModel):
    """One cheaper, capability-compatible alternative to the incumbent model."""

    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    blended_per_mtok_usd: float
    savings_pct: float
    max_input_tokens: int | None = None
    supports_tools: bool = False
    supports_vision: bool = False
    # Same vendor as the incumbent — the most credible "just as good" swaps, and the
    # ones that don't need a new API key. Surfaced first.
    same_provider: bool = False

    @property
    def ref(self) -> str:
        """The ``provider/model`` string a node's ``model_provider`` field expects."""
        return f"{self.provider}/{self.model}" if self.provider else self.model


class RightsizingResult(BaseModel):
    """Cheaper capability-compatible candidates for an incumbent model, or why there are none.

    ``incumbent_known`` is ``False`` when litellm has no pricing for the incumbent (e.g. a
    model newer than the installed litellm) — the honest signal that this is a *pricing gap*,
    not "nothing is cheaper". Never let an unknown incumbent read as "no savings available".
    """

    model_config = ConfigDict(extra="forbid")

    incumbent: str
    incumbent_known: bool
    incumbent_provider: str | None = None
    incumbent_blended_per_mtok_usd: float | None = None
    needs_tools: bool = False
    needs_vision: bool = False
    assumption: str = _BLEND_ASSUMPTION
    candidates: list[ModelOption] = Field(default_factory=list)
    note: str = ""


def _blended_per_token(input_cost: float, output_cost: float) -> float:
    """Weighted mean $/token under the documented input:output ratio."""
    return (_INPUT_WEIGHT * input_cost + _OUTPUT_WEIGHT * output_cost) / (
        _INPUT_WEIGHT + _OUTPUT_WEIGHT
    )


def _bare_name(model_key: str) -> str:
    """Canonical short model name — the last path segment (drops provider/route prefixes)."""
    return model_key.rsplit("/", maxsplit=1)[-1]


def _dated(name: str) -> bool:
    """True when the model name carries a dated-snapshot / rolling-alias suffix."""
    return bool(_DATE_SUFFIX.search(name))


def _family(name: str) -> str:
    """The model name with any dated-snapshot suffix stripped — its grouping key."""
    return _DATE_SUFFIX.sub("", name)


def describe(model: str) -> ModelOption | None:
    """Build a :class:`ModelOption` for a single model (pricing + capability), or ``None``.

    Used to represent the *incumbent* itself with the same shape as its candidates — the
    measured experiment needs the incumbent's input/output prices to project cost. Returns
    ``None`` when litellm has no pricing for the model. ``savings_pct`` is 0 and
    ``same_provider`` is True by definition (it is its own reference).
    """
    try:
        import litellm
    except Exception:  # pragma: no cover - defensive
        return None
    info: dict | None = None
    for lookup in dict.fromkeys((model, _bare_name(model))):
        try:
            info = dict(litellm.get_model_info(lookup))
            break
        except Exception:
            continue
    if not info:
        return None
    input_cost = info.get("input_cost_per_token") or 0.0
    output_cost = info.get("output_cost_per_token") or 0.0
    provider = info.get("litellm_provider") or (model.split("/", 1)[0] if "/" in model else "")
    return ModelOption(
        model=_bare_name(model),
        provider=provider,
        input_per_mtok_usd=round(input_cost * 1_000_000, 4),
        output_per_mtok_usd=round(output_cost * 1_000_000, 4),
        blended_per_mtok_usd=round(_blended_per_token(input_cost, output_cost) * 1_000_000, 4),
        savings_pct=0.0,
        max_input_tokens=info.get("max_input_tokens"),
        supports_tools=bool(info.get("supports_function_calling")),
        supports_vision=bool(info.get("supports_vision")),
        same_provider=True,
    )


def recommend(
    incumbent: str,
    *,
    needs_tools: bool = False,
    needs_vision: bool = False,
    min_savings_pct: float = 20.0,
    limit: int = 6,
) -> RightsizingResult:
    """Return cheaper, capability-compatible alternatives to ``incumbent``.

    Capability is a *gate*, applied before price: a candidate that can't call the node's
    tools (``needs_tools``) or accept images (``needs_vision``) is disqualified at any
    price — cheaper-but-can't-do-the-job is not a saving. Among the survivors, only those
    at least ``min_savings_pct`` cheaper (blended) than the incumbent are returned, sorted
    same-provider-first then cheapest, capped at ``limit``.

    Pure and side-effect-free apart from reading litellm's in-memory model DB. An unknown
    incumbent yields ``incumbent_known=False`` and no candidates (never an exception), so a
    brand-new model degrades to "we can't price this yet" rather than a 500.
    """
    limit = max(1, min(limit, 20))
    try:
        import litellm
    except Exception:  # pragma: no cover - litellm is a hard dep, defensive only
        return RightsizingResult(
            incumbent=incumbent,
            incumbent_known=False,
            needs_tools=needs_tools,
            needs_vision=needs_vision,
            note="Pricing data is unavailable (litellm not importable).",
        )

    # Resolve the incumbent as given (e.g. "openai/gpt-4o"), then fall back to the bare
    # name — litellm maps some models only under one form or the other.
    incumbent_info: dict | None = None
    for lookup in dict.fromkeys((incumbent, _bare_name(incumbent))):
        try:
            incumbent_info = dict(litellm.get_model_info(lookup))
            break
        except Exception:
            continue

    if not incumbent_info:
        return RightsizingResult(
            incumbent=incumbent,
            incumbent_known=False,
            needs_tools=needs_tools,
            needs_vision=needs_vision,
            note=(
                f"No pricing on record for {incumbent!r} — it may be newer than the "
                "installed model database. Can't compare it to alternatives yet."
            ),
        )

    inc_input = incumbent_info.get("input_cost_per_token") or 0.0
    inc_output = incumbent_info.get("output_cost_per_token") or 0.0
    inc_provider = incumbent_info.get("litellm_provider") or ""
    inc_blended = _blended_per_token(inc_input, inc_output)

    if inc_blended <= 0:
        return RightsizingResult(
            incumbent=incumbent,
            incumbent_known=True,
            incumbent_provider=inc_provider or None,
            incumbent_blended_per_mtok_usd=0.0,
            needs_tools=needs_tools,
            needs_vision=needs_vision,
            note=(
                f"{incumbent!r} has no positive token price on record — already free or "
                "unpriced, so there's nothing cheaper to recommend."
            ),
        )

    ceiling = inc_blended * (1.0 - min_savings_pct / 100.0)
    inc_bare = _bare_name(incumbent)

    # Dedup by (provider, model family): litellm lists many aliases for one model
    # ("gpt-4o" / "openai/gpt-4o") and every dated snapshot ("gpt-5-nano" +
    # "gpt-5-nano-2025-08-07"). Collapse to one row per family, preferring the undated
    # alias for display and the cheapest price within the family.
    best: dict[tuple[str, str], ModelOption] = {}
    model_cost: dict = getattr(litellm, "model_cost", {}) or {}
    for key, info in model_cost.items():
        if not isinstance(info, dict) or key == "sample_spec":
            continue
        # "ft:…" rows are fine-tune *pricing templates*, not selectable base models.
        if key.startswith("ft:"):
            continue
        if info.get("mode") != "chat":
            continue
        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        if not input_cost or not output_cost:  # need a real, positive price on both
            continue
        supports_tools = bool(info.get("supports_function_calling"))
        supports_vision = bool(info.get("supports_vision"))
        if needs_tools and not supports_tools:
            continue
        if needs_vision and not supports_vision:
            continue

        blended = _blended_per_token(input_cost, output_cost)
        if blended > ceiling:  # not cheap enough to be worth a switch
            continue

        provider = info.get("litellm_provider") or (key.split("/", 1)[0] if "/" in key else "")
        bare = _bare_name(key)
        if provider == inc_provider and bare == inc_bare:
            continue  # the incumbent itself under an alias

        option = ModelOption(
            model=bare,
            provider=provider,
            input_per_mtok_usd=round(input_cost * 1_000_000, 4),
            output_per_mtok_usd=round(output_cost * 1_000_000, 4),
            blended_per_mtok_usd=round(blended * 1_000_000, 4),
            savings_pct=round((1.0 - blended / inc_blended) * 100.0, 1),
            max_input_tokens=info.get("max_input_tokens"),
            supports_tools=supports_tools,
            supports_vision=supports_vision,
            same_provider=provider == inc_provider,
        )
        dedup_key = (provider, _family(bare))
        current = best.get(dedup_key)
        if current is None:
            best[dedup_key] = option
            continue
        # Prefer the undated alias for display; among equal datedness, the cheaper one.
        new_is_better = (_dated(current.model), current.blended_per_mtok_usd) > (
            _dated(option.model),
            option.blended_per_mtok_usd,
        )
        if new_is_better:
            best[dedup_key] = option

    # Same-provider first (most credible, no new key), then cheapest blended.
    ranked = sorted(
        best.values(),
        key=lambda o: (not o.same_provider, o.blended_per_mtok_usd),
    )[:limit]

    if ranked:
        note = (
            "Capability-compatible and cheaper than your current model. These are "
            "candidates to A/B test on your real traffic — not a guarantee of equal "
            "quality. Same-provider options are listed first."
        )
    else:
        gate = " with the required capabilities" if (needs_tools or needs_vision) else ""
        note = (
            f"No chat model{gate} is at least {min_savings_pct:.0f}% cheaper than "
            f"{incumbent!r} — it already looks well-priced for this task."
        )

    return RightsizingResult(
        incumbent=incumbent,
        incumbent_known=True,
        incumbent_provider=inc_provider or None,
        incumbent_blended_per_mtok_usd=round(inc_blended * 1_000_000, 4),
        needs_tools=needs_tools,
        needs_vision=needs_vision,
        candidates=ranked,
        note=note,
    )
