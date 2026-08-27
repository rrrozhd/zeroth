"""Content-minimizing LangGraph callback recorder."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from threading import RLock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from zeroth.check.adapter.bindings import ToolRegistration
from zeroth.check.tape.models import ModelCallObservationV1, ToolOccurrenceV1
from zeroth.check.tape.normalization import (
    action_identity_v1,
    argument_fingerprint,
    sha256_digest,
)


@dataclass(slots=True)
class _ToolStart:
    name: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass(slots=True)
class _ModelStart:
    provider: str | None
    model: str | None
    request_fingerprint: str


class LangGraphRecordingHandler(BaseCallbackHandler):
    """Capture tool results and usage while discarding provider bodies and callback objects."""

    def __init__(
        self,
        *,
        registrations: dict[str, ToolRegistration],
        case_id: str,
        scenario_run_id: str,
    ) -> None:
        self._registrations = registrations
        self._case_id = case_id
        self._scenario_run_id = scenario_run_id
        self._starts: dict[str, _ToolStart] = {}
        self._tools: list[ToolOccurrenceV1] = []
        self._model_starts: dict[str, _ModelStart] = {}
        self._models: list[ModelCallObservationV1] = []
        self._lock = RLock()

    @property
    def tool_occurrences(self) -> tuple[ToolOccurrenceV1, ...]:
        with self._lock:
            return tuple(self._tools)

    @property
    def model_calls(self) -> tuple[ModelCallObservationV1, ...]:
        with self._lock:
            return tuple(self._models)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        invocation_params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._start_model(
            serialized=serialized,
            request=messages,
            run_id=run_id,
            metadata=metadata,
            invocation_params=invocation_params,
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        invocation_params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._start_model(
            serialized=serialized,
            request=prompts,
            run_id=run_id,
            metadata=metadata,
            invocation_params=invocation_params,
        )

    def _start_model(
        self,
        *,
        serialized: dict[str, Any],
        request: Any,
        run_id: UUID,
        metadata: dict[str, Any] | None,
        invocation_params: dict[str, Any] | None,
    ) -> None:
        metadata = metadata or {}
        invocation_params = invocation_params or {}
        provider = metadata.get("ls_provider")
        model = metadata.get("ls_model_name") or invocation_params.get("model")
        serialized_id = serialized.get("id")
        if not isinstance(provider, str) and type(serialized_id) is list and serialized_id:
            provider = str(serialized_id[-2]) if len(serialized_id) > 1 else None
        if not isinstance(provider, str) or not provider:
            provider = None
        if not isinstance(model, str) or not model:
            model = None
        with self._lock:
            self._model_starts[str(run_id)] = _ModelStart(
                provider=provider,
                model=model,
                request_fingerprint=sha256_digest(_message_shape(request)),
            )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        with self._lock:
            start = self._model_starts.pop(str(run_id), None)
            if start is None:
                return
            message = _first_generation_message(response)
            usage = getattr(message, "usage_metadata", None)
            if type(usage) is not dict:
                usage = _legacy_usage(response)
            response_metadata = getattr(message, "response_metadata", {})
            provider = start.provider
            model = start.model
            if type(response_metadata) is dict:
                provider = provider or response_metadata.get("provider")
                model = (
                    model or response_metadata.get("model_name") or response_metadata.get("model")
                )
            usage = usage if type(usage) is dict else {}
            self._models.append(
                ModelCallObservationV1(
                    occurrence_id=f"model-{len(self._models) + 1:04d}",
                    provider=provider,
                    model=model,
                    input_tokens=_nonnegative_int(usage.get("input_tokens")),
                    output_tokens=_nonnegative_int(usage.get("output_tokens")),
                    total_tokens=_nonnegative_int(usage.get("total_tokens")),
                    input_details=_detail_map(usage.get("input_token_details")),
                    output_details=_detail_map(usage.get("output_token_details")),
                    request_fingerprint=start.request_fingerprint,
                    response_fingerprint=sha256_digest(_message_shape(message)),
                )
            )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del error, kwargs
        with self._lock:
            start = self._model_starts.pop(str(run_id), None)
            if start is None:
                return
            self._models.append(
                ModelCallObservationV1(
                    occurrence_id=f"model-{len(self._models) + 1:04d}",
                    provider=start.provider,
                    model=start.model,
                    request_fingerprint=start.request_fingerprint,
                    response_fingerprint=sha256_digest({"error": True}),
                )
            )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del input_str
        name = serialized.get("name")
        tool_call_id = kwargs.get("tool_call_id")
        if not isinstance(name, str) or name not in self._registrations:
            return
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        arguments = inputs if type(inputs) is dict else {}
        with self._lock:
            self._starts[str(run_id)] = _ToolStart(name, arguments, tool_call_id)

    def _finish(self, run_id: UUID, *, result: Any, error_type: str | None) -> None:
        with self._lock:
            start = self._starts.pop(str(run_id), None)
            if start is None:
                return
            registration = self._registrations[start.name]
            fingerprint = argument_fingerprint(start.arguments)
            occurrence_id = f"tool-{len(self._tools) + 1:04d}"
            self._tools.append(
                ToolOccurrenceV1(
                    occurrence_id=occurrence_id,
                    name=start.name,
                    input_schema_digest=registration.input_schema_digest,
                    tool_call_id=start.tool_call_id,
                    arguments=start.arguments,
                    argument_fingerprint=fingerprint,
                    side_effect=registration.side_effect,
                    result_available=error_type is None,
                    result=result if error_type is None else None,
                    error_type=error_type,
                    action_identity=action_identity_v1(
                        case_id=self._case_id,
                        scenario_run_id=self._scenario_run_id,
                        tool_name=start.name,
                        input_schema_digest=registration.input_schema_digest,
                        tool_call_id=start.tool_call_id,
                        argument_fingerprint=fingerprint,
                    ),
                )
            )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        content = getattr(output, "content", output)
        if isinstance(content, str):
            with suppress(json.JSONDecodeError):
                content = json.loads(content)
        self._finish(run_id, result=content, error_type=None)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, result=None, error_type=type(error).__name__)


def model_observation_from_metadata(
    *,
    occurrence_id: str,
    provider: str,
    model: str,
    usage: dict[str, Any],
    request_shape: Any,
    response_shape: Any,
) -> ModelCallObservationV1:
    """Normalize a provider callback's content-free usage projection."""
    return ModelCallObservationV1(
        occurrence_id=occurrence_id,
        provider=provider,
        model=model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        input_details=usage.get("input_token_details", {}),
        output_details=usage.get("output_token_details", {}),
        request_fingerprint=sha256_digest(request_shape),
        response_fingerprint=sha256_digest(response_shape),
    )


def _message_shape(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if type(value) is list:
        return [_message_shape(item) for item in value]
    if type(value) is dict:
        return {key: _message_shape(item) for key, item in value.items()}
    return {
        "type": getattr(value, "type", type(value).__name__),
        "content": _message_shape(getattr(value, "content", None)),
        "tool_calls": _message_shape(getattr(value, "tool_calls", [])),
    }


def _first_generation_message(response: Any) -> Any:
    generations = getattr(response, "generations", [])
    if generations and generations[0]:
        generation = generations[0][0]
        return getattr(generation, "message", generation)
    return None


def _legacy_usage(response: Any) -> dict[str, Any] | None:
    llm_output = getattr(response, "llm_output", None)
    if type(llm_output) is not dict or type(llm_output.get("token_usage")) is not dict:
        return None
    legacy = llm_output["token_usage"]
    return {
        "input_tokens": legacy.get("prompt_tokens"),
        "output_tokens": legacy.get("completion_tokens"),
        "total_tokens": legacy.get("total_tokens"),
    }


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _detail_map(value: Any) -> dict[str, int] | None:
    if type(value) is not dict:
        return None
    if any(type(item) is not int or item < 0 for item in value.values()):
        return None
    return dict(value)
