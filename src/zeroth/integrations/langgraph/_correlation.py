"""Carry the gateway correlation id from the reserved-context token to callbacks.

A plain ``BaseCallbackHandler`` cannot read ``config["configurable"]["_zeroth"]``
at callback time: LangGraph promotes only an allowlist of ``configurable`` keys
into the callback ``metadata`` kwarg, and ``_zeroth`` is never on it. The wrapper
therefore extracts the correlation id from the token *up front* and publishes it
on a **module-private ``ContextVar`` that only this package owns** for the
duration of each run; the handler reads it back off that ``ContextVar`` at every
callback.

The carrier is deliberately *not* ``config["metadata"]``. Metadata is a
caller-reachable channel -- a caller could forge a correlation via
``config["metadata"]``, via a callback-manager's ``metadata`` /
``inheritable_metadata``, or by a preceding list callback mutating metadata at
runtime -- and sanitizing each of those paths is whack-a-mole that still cannot
close the runtime-mutation path. A ``ContextVar`` set only by
:class:`~zeroth.integrations.langgraph._wrapper.GovernedGraph` and never exposed
to callers has no forge surface at all: whatever a caller places in metadata is
simply never read.

The extracted value is **UNVERIFIED**: it is base64url-decoded from the token's
payload segment without any signature check (verifying would need a
``SigningKeyProvider``, the expected audience and the deployment ref, none of
which exist in-process). It exists only to correlate captured spans; enforced
mode must never inherit trust from it. Extraction is fully defensive -- any
malformed or absent token yields ``None`` and never raises.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import json
from collections.abc import Mapping
from typing import Any

_CORRELATION_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "zeroth_langgraph_correlation", default=None
)
_RESERVED_TOKEN_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "zeroth_langgraph_reserved_token", default=None
)
"""The wrapper-owned carrier for the current run's UNVERIFIED correlation id.

Module-private and never reachable by callers: :class:`GovernedGraph` sets it
from the extracted token around each run (and resets it afterwards), and the
governance handler reads it at every callback via :func:`current_correlation`.
Because it is a ``ContextVar``, each top-level run -- thread or asyncio task --
sees only the value set in its own context (child worker threads / tasks inherit
a copy), so concurrent runs never cross-contaminate. Its default is ``None`` so a
run with no valid token, and any read outside a governed run, resolves to ``None``.
"""

_MAX_TOKEN_BYTES = 8192
"""Reject reserved-context tokens larger than this *before* decoding them.

A genuine gateway token is a few hundred characters; an oversized or deeply
nested payload is an input-triggered denial-of-service vector (a large base64
decode, then a deep ``json.loads`` that recurses in the C scanner and raises
``RecursionError``). Capping the raw token length first short-circuits both --
anything past this bound resolves to ``None`` without being decoded, so no
attacker-controlled work is performed. Sized well above any legitimate token yet
far below an abusive one.
"""


def _claim_from_token(token: str, name: str) -> str | None:
    """Return one bounded, UNVERIFIED string claim from a reserved-context token.

    The token is a compact ``header.payload.signature`` (base64url, unpadded).
    Only the middle payload segment is decoded and JSON-parsed; the signature is
    never checked. Returns ``None`` for any shape that is not a token carrying a
    string ``correlation_id``, and never raises: oversized tokens are rejected by
    length before decoding, and every parse failure -- including ``RecursionError``
    from a deeply nested payload -- is swallowed into ``None``.
    """
    if len(token) > _MAX_TOKEN_BYTES:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _correlation_from_token(token: str) -> str | None:
    """Return the UNVERIFIED ``correlation_id`` from a reserved-context token."""
    return _claim_from_token(token, "correlation_id")


def governance_run_id_from_token(token: str) -> str | None:
    """Return the UNVERIFIED run nonce carried by the token, or its correlation."""
    return _claim_from_token(token, "run_id") or _claim_from_token(token, "correlation_id")


def request_identity_from_token(token: str) -> tuple[str, str] | None:
    """Return the UNVERIFIED principal and policy revision carried by the token."""
    principal_id = _claim_from_token(token, "principal_id")
    policy_version = _claim_from_token(token, "policy_version")
    if principal_id is None or policy_version is None:
        return None
    return principal_id, policy_version


def _correlation_from_config(config: Any) -> str | None:
    """Return the UNVERIFIED gateway correlation id carried by ``config``, or ``None``.

    Reads ``config["configurable"]["_zeroth"]`` (the gateway-injected reserved
    context token) and extracts ``correlation_id`` from its payload without
    signature verification -- see the module docstring. Absent or malformed at
    any step (no config, no configurable, no token, non-string token, unparseable
    payload) yields ``None`` and never raises. This value is UNVERIFIED and must
    not be treated as authenticated.
    """
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    token = configurable.get("_zeroth")
    if not isinstance(token, str):
        return None
    return _correlation_from_token(token)


def _reserved_token_from_config(config: Any) -> str | None:
    """Return the raw token only when its bounded payload carries a correlation id."""
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    token = configurable.get("_zeroth")
    if not isinstance(token, str) or _correlation_from_token(token) is None:
        return None
    return token


def correlation_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Return the UNVERIFIED correlation id carried by a run's config, or ``None``.

    LangGraph entrypoints accept the ``RunnableConfig`` either as a ``config=``
    keyword or as the second positional argument (or omit it entirely). This
    locates whichever form the caller used and extracts the correlation from it
    via :func:`_correlation_from_config`; with no config there is nothing to carry
    and the result is ``None``. Extract-only: neither ``args`` nor ``kwargs`` is
    read for anything else and nothing is mutated.
    """
    if "config" in kwargs:
        return _correlation_from_config(kwargs["config"])
    if len(args) >= 2:
        return _correlation_from_config(args[1])
    return None


def reserved_token_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Return the bounded raw reserved token carried by a run's config."""
    if "config" in kwargs:
        return _reserved_token_from_config(kwargs["config"])
    if len(args) >= 2:
        return _reserved_token_from_config(args[1])
    return None


def set_correlation(correlation_id: str | None) -> contextvars.Token[str | None]:
    """Publish ``correlation_id`` on the wrapper-owned carrier for the current context.

    Always sets (even ``None``) so a governed run with no valid token, or one
    nested inside another governed run, reads its *own* value rather than
    inheriting a stale one. Returns the reset token the caller must pass to
    :func:`reset_correlation` in a ``finally``.
    """
    return _CORRELATION_VAR.set(correlation_id)


def reset_correlation(token: contextvars.Token[str | None]) -> None:
    """Restore the carrier to the value it held before the paired :func:`set_correlation`."""
    _CORRELATION_VAR.reset(token)


def set_reserved_context_token(value: str | None) -> contextvars.Token[str | None]:
    """Publish the raw token only for the active governed delegate call."""
    return _RESERVED_TOKEN_VAR.set(value)


def reset_reserved_context_token(token: contextvars.Token[str | None]) -> None:
    """Restore the previous private reserved-token carrier value."""
    _RESERVED_TOKEN_VAR.reset(token)


def current_reserved_context_token() -> str | None:
    """Return the raw token for the active governed run, or ``None``."""
    return _RESERVED_TOKEN_VAR.get()


def current_correlation() -> str | None:
    """Return the correlation id published for the current context, or ``None``.

    Read by the governance handler at each callback. ``None`` whenever no governed
    run is active in this context or the active run carried no valid token.
    """
    return _CORRELATION_VAR.get()


__all__ = [
    "correlation_from_call",
    "current_reserved_context_token",
    "current_correlation",
    "governance_run_id_from_token",
    "request_identity_from_token",
    "reserved_token_from_call",
    "reset_correlation",
    "reset_reserved_context_token",
    "set_correlation",
    "set_reserved_context_token",
    "_correlation_from_config",
    "_reserved_token_from_config",
]
