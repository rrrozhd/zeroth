"""Helpers for passing data into and getting data out of executable units.

"Input injection" means taking structured data and converting it into
something an executable unit can consume (stdin, CLI args, env vars, or a file).
"Output extraction" means taking raw output (stdout, files, exit codes) and
turning it back into structured data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from zeroth.core.execution_units.models import InputMode, OutputMode

_INPUT_ENV_PREFIX = "ZEROTH_INPUT"
_OUTPUT_JSON_TAG = "ZEROTH_OUTPUT_JSON="


class ExecutionIOError(ValueError):
    """Base error for all input/output problems in executable units."""


class InputInjectionError(ExecutionIOError):
    """Raised when we cannot convert the input data into the required format."""


class OutputExtractionError(ExecutionIOError):
    """Raised when we cannot read or parse the output from an executable unit."""


class OutputConversionError(ExecutionIOError):
    """Raised when the extracted output does not match the expected data model."""


@dataclass(frozen=True, slots=True)
class InjectedInput:
    """Holds the prepared input data in whatever form the executable unit needs.

    Depending on the input mode, one or more of these fields will be populated:
    stdin for piped JSON, argv for CLI arguments, env for environment variables,
    or input_file for a JSON file on disk.
    """

    stdin: str | None = None
    argv: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    input_file: Path | None = None


@dataclass(frozen=True, slots=True)
class ExtractedOutput:
    """Holds the parsed output from an executable unit after it finishes.

    The `payload` field contains the actual structured data extracted from
    the output. The other fields preserve the raw stdout, stderr, exit code,
    and output file path for debugging.
    """

    payload: Any
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    output_file: Path | None = None


def inject_input(
    mode: InputMode | str,
    payload: BaseModel | Mapping[str, Any],
    *,
    input_file_path: Path | None = None,
    env_prefix: str = _INPUT_ENV_PREFIX,
) -> InjectedInput:
    """Convert structured data into the format an executable unit expects.

    For example, if the mode is "json_stdin", this serializes the payload
    as JSON and puts it in the stdin field. If the mode is "cli_args", it
    converts each key-value pair into --key value arguments.
    """
    input_mode = _coerce_input_mode(mode)
    data = _normalize_payload(payload)

    if input_mode is InputMode.JSON_STDIN:
        return InjectedInput(stdin=_dump_json(data))
    if input_mode is InputMode.CLI_ARGS:
        return InjectedInput(argv=tuple(_build_cli_args(data)))
    if input_mode is InputMode.ENV_VARS:
        return InjectedInput(env=_build_env_vars(data, env_prefix=env_prefix))
    if input_mode is InputMode.INPUT_FILE_JSON:
        if input_file_path is None:
            raise InputInjectionError("input_file_path is required for input_file_json mode")
        _write_json_file(input_file_path, data)
        return InjectedInput(
            env={f"{env_prefix}_FILE": str(input_file_path)},
            input_file=input_file_path,
        )
    raise InputInjectionError(f"unsupported input mode: {input_mode.value}")


def extract_output(
    mode: OutputMode | str,
    *,
    stdout: str,
    stderr: str = "",
    exit_code: int | None = None,
    output_file_path: Path | None = None,
) -> ExtractedOutput:
    """Parse raw output from an executable unit into structured data.

    Supports multiple modes: reading JSON from stdout, finding a tagged JSON
    line in stdout, reading a JSON file, capturing plain text, or just
    using the exit code.
    """
    output_mode = _coerce_output_mode(mode)

    if output_mode is OutputMode.JSON_STDOUT:
        return ExtractedOutput(
            payload=_parse_json(stdout),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
    if output_mode is OutputMode.TAGGED_STDOUT_JSON:
        return ExtractedOutput(
            payload=_parse_tagged_json(stdout),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
    if output_mode is OutputMode.OUTPUT_FILE_JSON:
        if output_file_path is None:
            raise OutputExtractionError("output_file_path is required for output_file_json mode")
        return ExtractedOutput(
            payload=_parse_json_file(output_file_path),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            output_file=output_file_path,
        )
    if output_mode is OutputMode.TEXT_STDOUT:
        return ExtractedOutput(
            payload={"text": stdout},
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
    if output_mode is OutputMode.EXIT_CODE_ONLY:
        if exit_code is None:
            raise OutputExtractionError("exit_code is required for exit_code_only mode")
        return ExtractedOutput(
            payload={"exit_code": exit_code},
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
    raise OutputExtractionError(f"unsupported output mode: {output_mode.value}")


def convert_output(
    output_model: type[BaseModel],
    extracted: ExtractedOutput | Any,
) -> BaseModel:
    """Validate extracted output data against a Pydantic model.

    Takes the raw payload from extract_output and makes sure it matches
    the expected output schema. Returns a validated Pydantic model instance.
    """
    payload = extracted.payload if isinstance(extracted, ExtractedOutput) else extracted
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    try:
        return output_model.model_validate(payload)
    except ValidationError as exc:
        raise OutputConversionError(str(exc)) from exc


def _normalize_payload(payload: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    """Turn a Pydantic model or mapping into a plain dict for serialization."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, Mapping):
        return dict(payload)
    raise InputInjectionError(f"unsupported payload type: {type(payload)!r}")


def _build_cli_args(payload: Mapping[str, Any]) -> list[str]:
    """Convert a dict into a flat list of --key value CLI arguments."""
    args: list[str] = []
    for key, value in payload.items():
        args.extend([f"--{key}", _stringify_cli_value(value)])
    return args


def _build_env_vars(payload: Mapping[str, Any], *, env_prefix: str) -> dict[str, str]:
    """Convert a dict into environment variables with a common prefix."""
    env: dict[str, str] = {}
    for key, value in payload.items():
        env[f"{env_prefix}_{_normalize_env_key(key)}"] = _dump_json(value)
    return env


def _dump_json(value: Any) -> str:
    """Serialize a value to a JSON string."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:  # pragma: no cover - defensive guard
        raise InputInjectionError(f"value is not JSON serializable: {value!r}") from exc


def _parse_json(raw: str) -> Any:
    """Parse a JSON string, raising OutputExtractionError on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OutputExtractionError("stdout is not valid JSON") from exc


def _parse_json_file(path: Path) -> Any:
    """Read and parse a JSON file from disk."""
    if not path.exists():
        raise OutputExtractionError(f"output file does not exist: {path}")
    try:
        return _parse_json(path.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise OutputExtractionError(f"failed to read output file: {path}") from exc


def _parse_tagged_json(stdout: str) -> Any:
    """Find and parse a ZEROTH_OUTPUT_JSON= tagged line from stdout."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith(_OUTPUT_JSON_TAG):
            continue
        return _parse_json(stripped[len(_OUTPUT_JSON_TAG) :].strip())
    raise OutputExtractionError(f"no {_OUTPUT_JSON_TAG.rstrip('=')} line found in stdout")


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a dict as formatted JSON to a file, creating parent dirs if needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise InputInjectionError(f"failed to write input file: {path}") from exc


def _normalize_env_key(key: str) -> str:
    """Convert a key name into a valid uppercase environment variable suffix."""
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", key).strip("_").upper()
    return normalized or "VALUE"


def _stringify_cli_value(value: Any) -> str:
    """Convert a value to a string suitable for use as a CLI argument."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _coerce_input_mode(mode: InputMode | str) -> InputMode:
    """Convert a string to an InputMode enum, raising on invalid values."""
    try:
        return mode if isinstance(mode, InputMode) else InputMode(mode)
    except ValueError as exc:
        raise InputInjectionError(f"unsupported input mode: {mode!r}") from exc


def _coerce_output_mode(mode: OutputMode | str) -> OutputMode:
    """Convert a string to an OutputMode enum, raising on invalid values."""
    try:
        return mode if isinstance(mode, OutputMode) else OutputMode(mode)
    except ValueError as exc:
        raise OutputExtractionError(f"unsupported output mode: {mode!r}") from exc
