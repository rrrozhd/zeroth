"""Pinned per-million-token rates for the recipe-tested model set.

The values are copied from the server catalog (litellm 1.98.0 through
``zeroth.econ.analytics.rightsizing.describe``) and checked for parity by
``tests/acceptance/phase3_integrations/test_contract.py``. A model that is not
listed prices as ``None``: the caller records the physical call as unmeasured,
never as zero. Recipes advertise only the listed models as priced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

RATE_CARD_VERSION = "2026-09-10/litellm-1.98.0"
_MTOK = Decimal(1_000_000)
_PLACES = Decimal("0.00000001")


@dataclass(frozen=True)
class Rates:
    provider: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None
    cache_write_per_mtok: Decimal | None


_CARD: dict[str, tuple[str, str, str, str | None, str | None]] = {
    # model: (provider, input, output, cache read, cache write) USD per million tokens
    "gpt-4.1": ("openai", "2.0", "8.0", "0.5", None),
    "gpt-4.1-mini": ("openai", "0.4", "1.6", "0.1", None),
    "claude-sonnet-4-20250514": ("anthropic", "3.0", "15.0", "0.3", "3.75"),
    "claude-haiku-4-5-20251001": ("anthropic", "1.0", "5.0", "0.1", "1.25"),
}


def bare_model(model: str) -> str:
    """Strip a ``provider/`` prefix; rates are keyed by the provider's model id."""
    return model.split("/", 1)[1] if "/" in model else model


_DATE_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8}|latest)$")


def rates(model: str) -> Rates | None:
    """Rates for a model id, exact first, then its undated alias (``gpt-4.1-mini-2025-04-14``)."""
    name = bare_model(model)
    entry = _CARD.get(name) or _CARD.get(_DATE_SUFFIX.sub("", name))
    if entry is None:
        return None
    provider, inp, out, cache_read, cache_write = entry
    return Rates(
        provider=provider,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_read_per_mtok=None if cache_read is None else Decimal(cache_read),
        cache_write_per_mtok=None if cache_write is None else Decimal(cache_write),
    )


def price(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal | None:
    """Estimated USD for one physical call, or ``None`` when any rate is unknown.

    ``input_tokens`` excludes cache-read and cache-write tokens; adapters
    normalise provider usage to that split before pricing. The result is
    quantized to eight decimals (half-even), the wire precision of ``cost_usd``.
    """
    card = rates(model)
    if card is None:
        return None
    total = Decimal(input_tokens) * card.input_per_mtok
    total += Decimal(output_tokens) * card.output_per_mtok
    for tokens, rate in (
        (cache_read_tokens, card.cache_read_per_mtok),
        (cache_write_tokens, card.cache_write_per_mtok),
    ):
        if tokens:
            if rate is None:
                return None
            total += Decimal(tokens) * rate
    return (total / _MTOK).quantize(_PLACES, rounding=ROUND_HALF_EVEN)
