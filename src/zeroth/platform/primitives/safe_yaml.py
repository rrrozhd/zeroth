"""The single sanctioned door for parsing YAML that somebody else wrote.

Nothing else in this codebase imports :mod:`yaml`, and that is deliberate: a
repository manifest arrives from an arbitrary git checkout, so the parser is
attacker-facing. Every YAML hazard is closed here, once, structurally:

* **Alias expansion** (the billion-laughs bomb) is refused at the *event*
  level -- a pre-parse scan over ``yaml.parse`` events rejects any
  ``AliasEvent`` before composition, so no anchor is ever expanded at all.
  The manifest schema has no legitimate use for anchors.
* **Streams, floods, and nesting** are bounded the same way: more than one
  document, more than 10,000 parse events, or mapping/sequence nesting deeper
  than 16 levels is refused before any object is constructed.
* **Duplicate keys** -- which PyYAML silently last-wins, letting one manifest
  read two ways to two parsers -- are refused by a ``SafeLoader`` subclass.

The error discipline follows :mod:`zeroth.platform.primitives.error_vocabulary`:
PyYAML's exception text is never propagated, because it quotes the hostile
document back at whoever reads the error (a log line, an HTTP response, a
validation report). Messages are rendered purely from this module's own
templates plus the caller-chosen ``context`` string -- context names *what is
being parsed* and is written by calling code, never derived from input (the
same contract as :mod:`zeroth.platform.primitives.boundary`). Parser causes are
dropped (``raise ... from None``) for the same reason: a chained cause carries
the quoted document into rendered tracebacks. Only the stable code and the
integer line/column survive.
"""

from __future__ import annotations

from enum import StrEnum

import yaml

__all__ = [
    "UntrustedYamlError",
    "UntrustedYamlErrorCode",
    "load_untrusted_yaml",
]

# Refused before construction: a manifest-sized document produces a few hundred
# events, so five figures of headroom only ever admits garbage.
_MAX_EVENTS = 10_000
# Mapping/sequence nesting a manifest could plausibly need is single digits.
_MAX_DEPTH = 16


class UntrustedYamlErrorCode(StrEnum):
    """Stable refusal codes for untrusted YAML. Safe for automated tooling."""

    YAML_TOO_LARGE = "yaml_too_large"
    YAML_NOT_UTF8 = "yaml_not_utf8"
    YAML_ALIAS_FORBIDDEN = "yaml_alias_forbidden"
    YAML_MULTIDOC_FORBIDDEN = "yaml_multidoc_forbidden"
    YAML_TOO_COMPLEX = "yaml_too_complex"
    YAML_TOO_DEEP = "yaml_too_deep"
    YAML_DUPLICATE_KEY = "yaml_duplicate_key"
    YAML_ROOT_NOT_MAPPING = "yaml_root_not_mapping"
    YAML_PARSE_ERROR = "yaml_parse_error"


# One fixed sentence per code. Nothing from the document may appear here; the
# only interpolations are limits this module owns and the caller's context.
_MESSAGES: dict[UntrustedYamlErrorCode, str] = {
    UntrustedYamlErrorCode.YAML_TOO_LARGE: "document exceeds the permitted size",
    UntrustedYamlErrorCode.YAML_NOT_UTF8: "document is not valid UTF-8",
    UntrustedYamlErrorCode.YAML_ALIAS_FORBIDDEN: "YAML anchors and aliases are not permitted",
    UntrustedYamlErrorCode.YAML_MULTIDOC_FORBIDDEN: "multiple YAML documents are not permitted",
    UntrustedYamlErrorCode.YAML_TOO_COMPLEX: (
        f"document exceeds the permitted budget of {_MAX_EVENTS} parse events"
    ),
    UntrustedYamlErrorCode.YAML_TOO_DEEP: (
        f"nesting exceeds the permitted depth of {_MAX_DEPTH} levels"
    ),
    UntrustedYamlErrorCode.YAML_DUPLICATE_KEY: "a mapping declares the same key more than once",
    UntrustedYamlErrorCode.YAML_ROOT_NOT_MAPPING: (
        "top-level value must be a mapping with string keys"
    ),
    UntrustedYamlErrorCode.YAML_PARSE_ERROR: "document is not parseable YAML",
}


class UntrustedYamlError(ValueError):
    """A refusal to load untrusted YAML, rendered without echoing the document.

    Attributes:
        code: The stable :class:`UntrustedYamlErrorCode`.
        line: 1-based line of the refusal, when the parser reported one.
        column: 1-based column of the refusal, when the parser reported one.
    """

    def __init__(
        self,
        code: UntrustedYamlErrorCode,
        *,
        context: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.code = code
        self.line = line
        self.column = column
        message = f"{context}: {_MESSAGES[code]}"
        if line is not None and column is not None:
            message += f" (line {line}, column {column})"
        super().__init__(message)


def _mark_position(mark: yaml.Mark | None) -> tuple[int | None, int | None]:
    """Convert a PyYAML mark (0-based) to a 1-based line/column pair."""
    if mark is None:
        return None, None
    return mark.line + 1, mark.column + 1


class _DuplicateKeyFoundError(Exception):
    """Internal signal from the loader; carries only the key's position."""

    def __init__(self, line: int | None, column: int | None) -> None:
        self.line = line
        self.column = column
        super().__init__()


class _DuplicateKeyDetectingLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that refuses mappings declaring the same key twice."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[object, object]:
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen: set[object] = set()
            for key_node, _value_node in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:
                    # Unhashable keys are refused by SafeConstructor below.
                    continue
                if duplicate:
                    line, column = _mark_position(key_node.start_mark)
                    raise _DuplicateKeyFoundError(line, column)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _scan_events(text: str, *, context: str) -> None:
    """Refuse aliases, extra documents, event floods, and deep nesting.

    Runs over parser *events*, before any composition or construction, so an
    alias is rejected as a token in the stream rather than expanded.
    """
    events = 0
    documents = 0
    depth = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            events += 1
            if events > _MAX_EVENTS:
                raise UntrustedYamlError(UntrustedYamlErrorCode.YAML_TOO_COMPLEX, context=context)
            line, column = _mark_position(event.start_mark)
            if isinstance(event, yaml.AliasEvent):
                raise UntrustedYamlError(
                    UntrustedYamlErrorCode.YAML_ALIAS_FORBIDDEN,
                    context=context,
                    line=line,
                    column=column,
                )
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise UntrustedYamlError(
                        UntrustedYamlErrorCode.YAML_MULTIDOC_FORBIDDEN,
                        context=context,
                        line=line,
                        column=column,
                    )
            elif isinstance(event, yaml.MappingStartEvent | yaml.SequenceStartEvent):
                depth += 1
                if depth > _MAX_DEPTH:
                    raise UntrustedYamlError(
                        UntrustedYamlErrorCode.YAML_TOO_DEEP,
                        context=context,
                        line=line,
                        column=column,
                    )
            elif isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
                depth -= 1
    except yaml.YAMLError as exc:
        line, column = _mark_position(getattr(exc, "problem_mark", None))
        # ``from None``: the PyYAML message quotes the document.
        raise UntrustedYamlError(
            UntrustedYamlErrorCode.YAML_PARSE_ERROR, context=context, line=line, column=column
        ) from None


def load_untrusted_yaml(data: bytes, *, max_bytes: int, context: str) -> dict[str, object]:
    """Parse untrusted YAML bytes into a plain dict, or refuse with a stable code.

    Args:
        data: The raw document bytes, exactly as fetched.
        max_bytes: The size cap, chosen by the calling code for its document
            kind (a manifest caller passes its manifest limit).
        context: What is being parsed, for the error message. Written by the
            *calling code*, never derived from input.

    Returns:
        The single document as a ``dict`` keyed by ``str``.

    Raises:
        UntrustedYamlError: The document is oversized, not UTF-8, uses aliases
            or multiple documents, exceeds the event or depth budget, declares
            a duplicate key, has a non-mapping (or non-string-keyed) root, or
            does not parse. Never carries text from the document.
    """
    if len(data) > max_bytes:
        raise UntrustedYamlError(UntrustedYamlErrorCode.YAML_TOO_LARGE, context=context)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # ``from None``: the UnicodeDecodeError message embeds document bytes.
        raise UntrustedYamlError(UntrustedYamlErrorCode.YAML_NOT_UTF8, context=context) from None

    _scan_events(text, context=context)

    try:
        loaded = yaml.load(text, Loader=_DuplicateKeyDetectingLoader)  # noqa: S506
    except _DuplicateKeyFoundError as exc:
        raise UntrustedYamlError(
            UntrustedYamlErrorCode.YAML_DUPLICATE_KEY,
            context=context,
            line=exc.line,
            column=exc.column,
        ) from None
    except yaml.YAMLError as exc:
        line, column = _mark_position(getattr(exc, "problem_mark", None))
        raise UntrustedYamlError(
            UntrustedYamlErrorCode.YAML_PARSE_ERROR, context=context, line=line, column=column
        ) from None

    # A string-keyed mapping is the only document shape any caller of this
    # door accepts, so an int-keyed root is refused here rather than left for
    # every downstream schema to re-discover.
    if type(loaded) is not dict or any(type(key) is not str for key in loaded):
        raise UntrustedYamlError(UntrustedYamlErrorCode.YAML_ROOT_NOT_MAPPING, context=context)
    return loaded
