"""Structured tool identities, and the digest the server computes over them.

Audit round 1 found that a registration carried its own ``coverage``,
``tool_count`` and ``inventory_fingerprint`` verbatim from the client, while
``tools`` held bare *names*. The provider then compared the registered
fingerprint against the attested one -- two strings the same client chose --
so a caller could register ``{coverage: "complete", tools: (), tool_count:
999, fingerprint: "X"}``, attest ``"X"``, and reach ``ENFORCED`` with no
governed tools at all. Both sides of the comparison were client-supplied.

This module removes the client from one side of it. A registration declares
*identities* -- a name and the fingerprint of the tool that answers to it --
and the aggregate digest and the count are **recomputed here** from those
identities. A caller can still lie about which tools exist; it can no longer
claim a digest that its declared tool set does not produce.

**The digest scheme is the SDK's, reproduced rather than imported.**
``zeroth.integrations.langgraph._tool_inventory.inventory_fingerprint``
computes the same value, and the two must agree or a truthful adapter's
attestation would never match its own registration. Governance may depend only
on ``{contracts, platform}``, so importing the SDK's implementation is not
open; instead the projection is restated here and
``tests/governance/test_inventory_trust_boundary.py`` pins the two against each
other, so a change to either that does not change the other fails.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field


class RegisteredTool(BaseModel):
    """One governed tool: the name a call arrives under, and what answers it.

    The fingerprint is what makes the identity meaningful. A registration
    holding names alone says only which labels exist, so a tool swapped for
    another under the same name would register identically -- exactly the
    substitution ZER-6's suite exists to catch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


def refuse_repeated_tool_names(tools: tuple[RegisteredTool, ...]) -> None:
    """Refuse a declared tool set that names the same tool twice.

    A repeated name defeats identity matching rather than merely duplicating a
    row. Admission compares the called ``(name, fingerprint)`` against the
    registered set, so a registration carrying both ``("send_email",
    "fp-legit")`` and ``("send_email", "fp-impostor")`` authorizes *both*
    callables under one governed label -- the impostor is admitted by standing
    next to the real tool, and the aggregate digest still recomputes cleanly
    because it is taken over whatever pairs were declared.

    The SDK already refuses this on its side
    (``zeroth.integrations.langgraph._tool_inventory``, whose module docstring
    states the doctrine: a repeated tool name is refused on both sides). This
    is the server half, so the rule holds against a caller that does not use
    the SDK at all.

    Args:
        tools: The declared identities, in declaration order.

    Raises:
        ValueError: If a name occurs more than once. Raised as ``ValueError``
            rather than a bespoke type so a pydantic validator carrying this
            rule reports it as an ordinary field error -- which is what makes
            it a 422 on the wire instead of an unhandled 500.
    """
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError("a tool inventory cannot name the same tool twice")
        seen.add(tool.name)


def recompute_inventory_fingerprint(tools: tuple[RegisteredTool, ...]) -> str:
    """Return the order-insensitive digest of a declared tool set.

    Mirrors the SDK's ``inventory_fingerprint``: the sorted ``[name,
    fingerprint]`` pairs are serialized under ``{"tools": ...}`` with sorted
    keys, compact separators and ASCII escaping, then hashed with SHA-256. Two
    inventories holding the same tools in different orders digest alike; one
    holding a same-named tool with a different fingerprint does not.

    Args:
        tools: The declared identities. Order is irrelevant.

    Returns:
        The hex SHA-256 digest of the canonical tool set.
    """
    pairs = sorted([tool.name, tool.fingerprint] for tool in tools)
    serialized = json.dumps(
        {"tools": pairs},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "RegisteredTool",
    "recompute_inventory_fingerprint",
    "refuse_repeated_tool_names",
]
