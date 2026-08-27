from __future__ import annotations

import json
import math

import pytest
from hypothesis import given, strategies as st

from zeroth.check.tape.normalization import (
    NormalizationError,
    canonical_bytes,
    canonical_loads,
    sha256_digest,
)


json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(), children, max_size=5)
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonicalization_is_idempotent(value: object) -> None:
    encoded = canonical_bytes(value)
    assert canonical_bytes(canonical_loads(encoded)) == encoded


@given(st.dictionaries(st.text(), json_scalars, max_size=8))
def test_object_key_order_does_not_change_bytes(value: dict[str, object]) -> None:
    assert canonical_bytes(value) == canonical_bytes(dict(reversed(list(value.items()))))


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf, {1: "value"}, b"bytes", {"set"}, object()],
)
def test_unsupported_values_are_rejected(value: object) -> None:
    with pytest.raises(NormalizationError):
        canonical_bytes(value)


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(NormalizationError, match="duplicate object key"):
        canonical_loads(b'{"key":1,"key":2}')


def test_scalars_keep_their_json_types() -> None:
    value = {"bool": True, "null": None, "number": 1, "string": "1"}
    assert json.loads(canonical_bytes(value)) == value
    assert sha256_digest(value).startswith("sha256:")
