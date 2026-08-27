"""Explicit tool bindings shared by record, replay, and fault workers."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_type_hints

from pydantic import TypeAdapter

from zeroth.check.tape.normalization import sha256_digest

SideEffect = Literal["read_only", "side_effecting"]
BindingMode = Literal["record", "replay", "fault"]


class BindingError(ValueError):
    """A target registered an invalid or unavailable tool binding."""


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    name: str
    side_effect: SideEffect
    input_schema: dict[str, Any]
    input_schema_digest: str
    implementation: object | None
    input_model: object | None = None


def _callable_schema(implementation: object) -> dict[str, Any]:
    args_schema = getattr(implementation, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        schema = args_schema.model_json_schema()
        if not isinstance(schema, dict):
            raise BindingError("tool input schema is not an object")
        return schema
    try:
        signature = inspect.signature(implementation)
        hints = get_type_hints(implementation)  # type: ignore[arg-type]
    except (TypeError, ValueError, NameError) as exc:
        raise BindingError("tool input schema cannot be inspected") from exc
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            raise BindingError("variadic tool callables have no stable input schema")
        annotation = hints.get(parameter.name)
        if annotation is None:
            raise BindingError(f"tool input schema missing annotation for {parameter.name}")
        properties[parameter.name] = TypeAdapter(annotation).json_schema()
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


class CheckBindings:
    """Worker-owned registry passed to a customer's ``build_target`` function."""

    def __init__(
        self,
        *,
        action_repository: object,
        mode: BindingMode = "record",
        replacements: Mapping[str, object] | None = None,
    ) -> None:
        if mode not in {"record", "replay", "fault"}:
            raise BindingError(f"unsupported binding mode: {mode}")
        self._action_repository = action_repository
        self._action_repository_requested = False
        self._mode = mode
        self._replacements = dict(replacements or {})
        self._registrations: dict[str, ToolRegistration] = {}
        self._frozen = False

    @property
    def action_repository(self) -> object:
        self._action_repository_requested = True
        return self._action_repository

    @property
    def action_repository_requested(self) -> bool:
        return self._action_repository_requested

    @property
    def registrations(self) -> Mapping[str, ToolRegistration]:
        return dict(self._registrations)

    def freeze(self) -> None:
        self._frozen = True

    def tool(
        self,
        name: str,
        implementation: object,
        side_effect: SideEffect,
    ) -> Any:
        if self._frozen:
            raise BindingError("tool registry is frozen")
        if not isinstance(name, str) or not name.strip():
            raise BindingError("tool name must be nonblank")
        if name in self._registrations:
            raise BindingError(f"duplicate tool name: {name}")
        if side_effect not in {"read_only", "side_effecting"}:
            raise BindingError("side_effect must be read_only or side_effecting")
        if not callable(implementation) and not hasattr(implementation, "args_schema"):
            raise BindingError("tool implementation must be callable and expose a schema")
        schema = _callable_schema(implementation)
        selected = implementation
        retained: object | None = implementation
        if self._mode != "record":
            try:
                selected = self._replacements[name]
            except KeyError as exc:
                raise BindingError(f"no taped implementation registered for {name}") from exc
            retained = None
        if (
            not callable(selected)
            and not hasattr(selected, "invoke")
            and not hasattr(selected, "bind_registration")
        ):
            raise BindingError(f"selected implementation for {name} is not executable")
        self._registrations[name] = ToolRegistration(
            name=name,
            side_effect=side_effect,
            input_schema=schema,
            input_schema_digest=sha256_digest(schema),
            implementation=retained,
            input_model=getattr(implementation, "args_schema", None),
        )
        registration = self._registrations[name]
        if self._mode != "record" and hasattr(selected, "bind_registration"):
            metadata_only = ToolRegistration(
                name=registration.name,
                side_effect=registration.side_effect,
                input_schema=registration.input_schema,
                input_schema_digest=registration.input_schema_digest,
                implementation=None,
                input_model=registration.input_model,
            )
            selected = selected.bind_registration(metadata_only)
        return selected
