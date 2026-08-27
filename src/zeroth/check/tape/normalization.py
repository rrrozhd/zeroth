"""NormalizationV1 canonical JSON and stable identity functions."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class NormalizationError(ValueError):
    """A value cannot be represented by NormalizationV1."""


def _normalize(value: Any, *, path: str = "$") -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise NormalizationError(f"non-finite number at {path}")
        return value
    if type(value) is list:
        return [_normalize(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise NormalizationError(f"non-string object key at {path}")
            normalized[key] = _normalize(item, path=f"{path}.{key}")
        return normalized
    raise NormalizationError(f"unsupported value {type(value).__name__} at {path}")


def canonical_bytes(value: Any) -> bytes:
    """Encode a JSON value according to NormalizationV1."""
    try:
        return json.dumps(
            _normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NormalizationError(str(exc)) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizationError(f"duplicate object key: {key}")
        result[key] = value
    return result


def canonical_loads(value: bytes | str) -> Any:
    """Decode JSON while rejecting duplicate keys and non-V1 values."""
    try:
        loaded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except NormalizationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(str(exc)) from exc
    return _normalize(loaded)


def sha256_digest(value: Any) -> str:
    """Return a tagged SHA-256 digest of canonical NormalizationV1 bytes."""
    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"


def argument_fingerprint(arguments: dict[str, Any]) -> str:
    """Fingerprint canonical tool arguments."""
    return sha256_digest(arguments)


def action_identity_v1(
    *,
    case_id: str,
    scenario_run_id: str,
    tool_name: str,
    input_schema_digest: str,
    tool_call_id: str,
    argument_fingerprint: str,
) -> str:
    """Compute ActionIdentityV1 from its exact logical preimage."""
    payload = {
        "schema_version": "action_identity.v1",
        "case_id": case_id,
        "scenario_run_id": scenario_run_id,
        "tool_name": tool_name,
        "input_schema_digest": input_schema_digest,
        "tool_call_id": tool_call_id,
        "argument_fingerprint": argument_fingerprint,
    }
    return f"actv1_{hashlib.sha256(canonical_bytes(payload)).hexdigest()}"
