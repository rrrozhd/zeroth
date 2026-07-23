"""Carry the gateway correlation id from the reserved-context token to callbacks.

A plain ``BaseCallbackHandler`` cannot read ``config["configurable"]["_zeroth"]``
at callback time: LangGraph promotes only an allowlist of ``configurable`` keys
into the callback ``metadata`` kwarg, and ``_zeroth`` is never on it. So the
wrapper extracts the correlation id from the token *up front* and merges it into
``config["metadata"]``, which LangGraph *does* inherit down to every node's
callback metadata. The handler then reads it back as ``zeroth_correlation_id``.

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
import json
from collections.abc import Mapping
from typing import Any

CORRELATION_METADATA_KEY = "zeroth_correlation_id"
"""The ``config["metadata"]`` key the wrapper injects and the handler reads."""

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


def _correlation_from_token(token: str) -> str | None:
    """Return the UNVERIFIED ``correlation_id`` from a reserved-context token.

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
    correlation_id = payload.get("correlation_id")
    return correlation_id if isinstance(correlation_id, str) else None


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


def _with_correlation_metadata(config: Any) -> Any:
    """Return ``config`` with a Zeroth-owned correlation id in ``metadata``.

    Trust boundary: the handler must never observe a ``zeroth_correlation_id`` that
    Zeroth did not itself inject. Any caller-supplied value under that key is
    therefore stripped from the effective ``metadata`` *first*; the key is then
    re-set only when a valid ``_zeroth`` token yielded a correlation id. So an
    untrusted caller cannot forge a correlation by pre-seeding
    ``metadata["zeroth_correlation_id"]`` -- with no (or a malformed) token the
    key is removed and the span carries ``None`` (never the caller's value, never
    a fabricated one).

    The rest of ``metadata`` is *merged*, never replaced, and neither the input
    config nor its nested mappings are mutated. A non-mapping config carries no
    metadata the handler can read and is returned unchanged.
    """
    if not isinstance(config, Mapping):
        return config
    correlation_id = _correlation_from_config(config)
    merged: dict[str, Any] = dict(config)
    metadata = dict(merged.get("metadata") or {})
    # Strip any caller-supplied (potentially forged) correlation before injecting.
    metadata.pop(CORRELATION_METADATA_KEY, None)
    if correlation_id is not None:
        metadata[CORRELATION_METADATA_KEY] = correlation_id
    # Write back whenever we sanitized an existing metadata mapping or injected a
    # value; leave a metadata-less config untouched when there is nothing to add.
    if metadata or "metadata" in merged:
        merged["metadata"] = metadata
    return merged


def inject_correlation_metadata(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Return ``(args, kwargs)`` with correlation merged into the run config's metadata.

    Locates the ``RunnableConfig`` wherever the caller placed it (``config=``
    keyword or second positional) and, if it carries a reserved-context token,
    merges ``{zeroth_correlation_id: <id>}`` into ``config["metadata"]`` so it
    rides LangGraph's native metadata inheritance to every node callback. When no
    config is present there is nothing to inject and the arguments are returned
    unchanged (no config is fabricated). Neither input is mutated.
    """
    if "config" in kwargs:
        return args, {**kwargs, "config": _with_correlation_metadata(kwargs["config"])}
    if len(args) >= 2:
        return (args[0], _with_correlation_metadata(args[1]), *args[2:]), kwargs
    return args, kwargs


__all__ = [
    "CORRELATION_METADATA_KEY",
    "inject_correlation_metadata",
    "_correlation_from_config",
]
