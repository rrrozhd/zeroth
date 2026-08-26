"""ZER-37: the single sanctioned door for parsing untrusted YAML bytes.

A repository manifest is author-supplied content fetched from somebody else's
git checkout, so the parser is exercised the way an attacker would drive it:
alias bombs, multi-document streams, unbounded nesting, event floods, duplicate
keys, and hostile text planted where an error message might echo it back.
"""

from __future__ import annotations

import pytest

from zeroth.platform.primitives import (
    UntrustedYamlError,
    UntrustedYamlErrorCode,
    load_untrusted_yaml,
)

CONTEXT = "repository manifest"
CANARY = "31337_EVIL_CANARY_PAYLOAD"
MAX_BYTES = 4096


def _refused(data: bytes, *, max_bytes: int = MAX_BYTES) -> UntrustedYamlError:
    """Load ``data``, require a refusal, and require the refusal to be mute."""
    with pytest.raises(UntrustedYamlError) as excinfo:
        load_untrusted_yaml(data, max_bytes=max_bytes, context=CONTEXT)
    assert CANARY not in str(excinfo.value)
    for arg in excinfo.value.args:
        assert CANARY not in str(arg)
    return excinfo.value


def test_happy_path_returns_a_plain_dict() -> None:
    document = (
        b"schema_version: 1\n"
        b"scripts:\n"
        b"  train:\n"
        b"    entry: scripts/train.py\n"
        b"    tags: [a, b]\n"
    )

    loaded = load_untrusted_yaml(document, max_bytes=MAX_BYTES, context=CONTEXT)

    assert loaded == {
        "schema_version": 1,
        "scripts": {"train": {"entry": "scripts/train.py", "tags": ["a", "b"]}},
    }
    assert type(loaded) is dict


def test_alias_is_refused() -> None:
    error = _refused(f"a: &{CANARY} 1\nb: *{CANARY}\n".encode())

    assert error.code is UntrustedYamlErrorCode.YAML_ALIAS_FORBIDDEN


def test_billion_laughs_is_refused_structurally() -> None:
    """The classic expansion bomb dies at its first alias, before any expansion."""
    document = (
        b"a: &a [lol, lol, lol, lol, lol, lol, lol, lol, lol]\n"
        b"b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        b"c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        b"d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
    )

    error = _refused(document)

    assert error.code is UntrustedYamlErrorCode.YAML_ALIAS_FORBIDDEN


def test_multiple_documents_are_refused() -> None:
    error = _refused(b"---\na: 1\n---\nb: 2\n")

    assert error.code is UntrustedYamlErrorCode.YAML_MULTIDOC_FORBIDDEN


def test_nesting_beyond_the_depth_cap_is_refused() -> None:
    document = ("key: " + "[" * 20 + "]" * 20 + "\n").encode()

    error = _refused(document)

    assert error.code is UntrustedYamlErrorCode.YAML_TOO_DEEP


def test_nesting_at_the_depth_cap_is_permitted() -> None:
    document = ("key: " + "[" * 15 + "]" * 15 + "\n").encode()

    loaded = load_untrusted_yaml(document, max_bytes=MAX_BYTES, context=CONTEXT)

    assert "key" in loaded


def test_event_flood_is_refused() -> None:
    document = b"key: [" + b"a, " * 10_100 + b"]\n"

    error = _refused(document, max_bytes=1_000_000)

    assert error.code is UntrustedYamlErrorCode.YAML_TOO_COMPLEX


def test_duplicate_top_level_key_is_refused() -> None:
    error = _refused(f"{CANARY}: 1\n{CANARY}: 2\n".encode())

    assert error.code is UntrustedYamlErrorCode.YAML_DUPLICATE_KEY
    assert error.line is not None
    assert error.column is not None


def test_duplicate_nested_key_is_refused() -> None:
    error = _refused(b"outer:\n  inner: 1\n  inner: 2\n")

    assert error.code is UntrustedYamlErrorCode.YAML_DUPLICATE_KEY


@pytest.mark.parametrize(
    "document",
    [b"- 1\n- 2\n", b"just a scalar\n", b"", b"1: keyed-by-int\n"],
)
def test_non_mapping_roots_are_refused(document: bytes) -> None:
    """A list, a scalar, an empty stream, and a non-string-keyed map all fail."""
    error = _refused(document)

    assert error.code is UntrustedYamlErrorCode.YAML_ROOT_NOT_MAPPING


def test_oversized_document_is_refused_before_parsing() -> None:
    error = _refused(f"key: {CANARY}\n".encode(), max_bytes=8)

    assert error.code is UntrustedYamlErrorCode.YAML_TOO_LARGE


def test_invalid_utf8_is_refused() -> None:
    error = _refused(b"\xff\xfe: 1\n")

    assert error.code is UntrustedYamlErrorCode.YAML_NOT_UTF8


def test_parse_error_carries_line_and_column() -> None:
    error = _refused(b"a: [1,\nb\n")

    assert error.code is UntrustedYamlErrorCode.YAML_PARSE_ERROR
    assert isinstance(error.line, int) and error.line >= 1
    assert isinstance(error.column, int) and error.column >= 1


def test_parse_error_never_echoes_document_text() -> None:
    """PyYAML's own message quotes the document; ours must not."""
    error = _refused(f"{CANARY}: [{CANARY}\n".encode())

    assert error.code is UntrustedYamlErrorCode.YAML_PARSE_ERROR


def test_error_message_names_the_context() -> None:
    error = _refused(b"- not a mapping\n")

    assert str(error).startswith(f"{CONTEXT}: ")
