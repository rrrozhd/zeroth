"""Capability-enforcement errors and helpers (WS-C).

The policy guard resolves a node's declared ``capability_bindings`` into an
*effective* (granted) capability set and stores it on the run. WS-C turns that
set from advisory metadata into a behavioral gate: before an agent invokes a
tool or touches memory, the granted set must cover the operation's required
capabilities, or the operation is denied.

Everything here is **fail-closed**: an empty or missing granted set denies any
operation that requires a capability. There is deliberately no "granted is None
-> skip" convention — whether enforcement is *active at all* is an orthogonal
decision made by the caller (it passes ``None`` only when the policy guard is
not wired). Once a set (even an empty one) reaches these helpers, they enforce.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from zeroth.governance.policy.models import Capability


class CapabilityDeniedError(PermissionError):
    """Raised when an operation requires capabilities the node was not granted.

    Subclasses :class:`PermissionError` so callers that already treat tool /
    memory failures as recoverable (feeding the error back to the model) keep
    working, while capability denials remain distinguishable by type.
    """

    def __init__(
        self,
        *,
        node_id: str,
        required: set[Capability],
        granted: set[Capability],
    ) -> None:
        self.node_id = node_id
        self.required = set(required)
        self.granted = set(granted)
        missing = sorted(cap.value for cap in (self.required - self.granted))
        required_labels = ", ".join(sorted(cap.value for cap in self.required)) or "(none)"
        granted_labels = ", ".join(sorted(cap.value for cap in self.granted)) or "(none)"
        super().__init__(
            f"capability denied for node {node_id!r}: "
            f"missing {', '.join(missing) or '(none)'}. "
            f"required [{required_labels}]; granted [{granted_labels}]."
        )

    @property
    def missing(self) -> set[Capability]:
        """The capabilities that were required but not granted."""
        return self.required - self.granted


def parse_effective_capabilities(ctx: Mapping[str, Any] | None) -> set[Capability]:
    """Read the effective (granted) capability set out of an enforcement context.

    The orchestrator stores the policy guard's ``effective_capabilities`` on the
    run as a JSON list of capability value strings and threads it back through
    ``enforcement_context``. This turns that list into a ``set[Capability]``.

    Unmapped strings (values that do not correspond to a known
    :class:`Capability`) are dropped: an unrecognized *grant* can never satisfy
    a known *required* capability, so dropping it keeps the result fail-closed
    (it grants nothing) without raising on forward-compatible extra values.

    Returns an **empty set** when the context is missing, has no
    ``effective_capabilities`` key, or lists nothing. Emptiness here means
    "nothing granted" (deny everything that needs a capability) — it does NOT
    mean "skip enforcement". The decision to skip is the caller's, signalled by
    passing ``None`` instead of calling this helper.
    """
    if not ctx:
        return set()
    raw = ctx.get("effective_capabilities")
    if not raw:
        return set()
    granted: set[Capability] = set()
    for value in raw:
        try:
            granted.add(Capability(value))
        except ValueError:
            # Unknown capability string: cannot grant any known capability.
            continue
    return granted


def require_capabilities(
    required: Iterable[Capability],
    effective: set[Capability] | None,
    *,
    node_id: str,
) -> None:
    """Assert that ``effective`` covers every capability in ``required``.

    Fail-closed: ``effective`` of ``None`` is treated as the empty set, so any
    non-empty ``required`` denies. When ``required`` is empty the call is a
    no-op (an operation that needs no capability is always allowed).

    Raises :class:`CapabilityDeniedError` listing the missing capabilities.
    """
    required_set = set(required)
    if not required_set:
        return
    granted = effective or set()
    if not required_set <= granted:
        raise CapabilityDeniedError(
            node_id=node_id,
            required=required_set,
            granted=granted,
        )


__all__ = [
    "CapabilityDeniedError",
    "parse_effective_capabilities",
    "require_capabilities",
]
