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


def _correlation_from_token(token: str) -> str | None:
    """Return the UNVERIFIED ``correlation_id`` from a reserved-context token.

    The token is a compact ``header.payload.signature`` (base64url, unpadded).
    Only the middle payload segment is decoded and JSON-parsed; the signature is
    never checked. Returns ``None`` for any shape that is not a token carrying a
    string ``correlation_id``.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError, TypeError):
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
    """Return ``config`` with the correlation id merged into ``metadata``.

    When no correlation id can be extracted the config is returned unchanged (the
    correlation id is never fabricated; such spans simply carry ``None``). The
    existing ``metadata`` mapping is *merged*, never replaced, and neither the
    input config nor its nested mappings are mutated.
    """
    correlation_id = _correlation_from_config(config)
    if correlation_id is None:
        return config
    merged: dict[str, Any] = dict(config)
    metadata = dict(merged.get("metadata") or {})
    metadata[CORRELATION_METADATA_KEY] = correlation_id
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
