from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from zeroth.execution_units.io import (
    ExtractedOutput,
    InputInjectionError,
    OutputConversionError,
    OutputExtractionError,
    convert_output,
    extract_output,
    inject_input,
)
from zeroth.execution_units.models import InputMode, OutputMode


class DemoInput(BaseModel):
    name: str
    count: int
    metadata: dict[str, str]
    api_key: str


class DemoOutput(BaseModel):
    answer: str
    score: int


@pytest.mark.parametrize(
    ("mode", "expected_stdin", "expected_argv", "expected_env_key", "expected_env_value"),
    [
        (
            InputMode.JSON_STDIN,
            {"name": "alpha", "count": 3, "metadata": {"role": "admin"}, "api_key": "secret"},
            (),
            None,
            None,
        ),
        (
            InputMode.CLI_ARGS,
            None,
            (
                "--name",
                "alpha",
                "--count",
                "3",
                "--metadata",
                '{"role": "admin"}',
                "--api_key",
                "secret",
            ),
            None,
            None,
        ),
        (
            InputMode.ENV_VARS,
            None,
            (),
            "ZEROTH_INPUT_API_KEY",
            '"secret"',
        ),
    ],
)
def test_input_injection_modes_render_structured_payload(
    mode: InputMode,
    expected_stdin: dict[str, object] | None,
    expected_argv: tuple[str, ...],
    expected_env_key: str | None,
    expected_env_value: str | None,
) -> None:
    injected = inject_input(
        mode,
        DemoInput(name="alpha", count=3, metadata={"role": "admin"}, api_key="secret"),
    )

    if expected_stdin is not None:
        assert json.loads(injected.stdin or "") == expected_stdin
    assert injected.argv == expected_argv
    if expected_env_key is not None:
        assert injected.env == {
            "ZEROTH_INPUT_NAME": '"alpha"',
            "ZEROTH_INPUT_COUNT": "3",
            "ZEROTH_INPUT_METADATA": '{"role": "admin"}',
            expected_env_key: expected_env_value,
        }


def test_input_file_json_mode_writes_payload(tmp_path: Path) -> None:
    input_file = tmp_path / "input.json"

    injected = inject_input(
        InputMode.INPUT_FILE_JSON,
        DemoInput(name="alpha", count=3, metadata={"role": "admin"}, api_key="secret"),
        input_file_path=input_file,
    )

    assert injected.input_file == input_file
    assert injected.env == {"ZEROTH_INPUT_FILE": str(input_file)}
    assert json.loads(input_file.read_text(encoding="utf-8")) == {
        "name": "alpha",
        "count": 3,
        "metadata": {"role": "admin"},
        "api_key": "secret",
    }


@pytest.mark.parametrize(
    ("mode", "stdout", "exit_code", "output_file", "expected_payload"),
    [
        (
            OutputMode.JSON_STDOUT,
            '  {"answer": "done", "score": 7}\n',
            None,
            None,
            {"answer": "done", "score": 7},
        ),
        (
            OutputMode.TAGGED_STDOUT_JSON,
            "log line\nZEROTH_OUTPUT_JSON={\"answer\":\"done\",\"score\":7}\n",
            None,
            None,
            {"answer": "done", "score": 7},
        ),
        (
            OutputMode.TEXT_STDOUT,
            "plain text output\n",
            None,
            None,
            {"text": "plain text output\n"},
        ),
        (
            OutputMode.EXIT_CODE_ONLY,
            "",
            17,
            None,
            {"exit_code": 17},
        ),
    ],
)
def test_output_extraction_modes_normalize_payload(
    mode: OutputMode,
    stdout: str,
    exit_code: int | None,
    output_file: Path | None,
    expected_payload: dict[str, object],
) -> None:
    extracted = extract_output(
        mode,
        stdout=stdout,
        exit_code=exit_code,
        output_file_path=output_file,
    )

    assert extracted.payload == expected_payload
    assert extracted.stdout == stdout
    assert extracted.exit_code == exit_code


def test_output_file_json_mode_reads_json_payload(tmp_path: Path) -> None:
    output_file = tmp_path / "output.json"
    output_file.write_text(json.dumps({"answer": "done", "score": 7}), encoding="utf-8")

    extracted = extract_output(
        OutputMode.OUTPUT_FILE_JSON,
        stdout="ignored",
        output_file_path=output_file,
    )

    assert extracted.output_file == output_file
    assert extracted.payload == {"answer": "done", "score": 7}


def test_output_conversion_validates_typed_model() -> None:
    extracted = ExtractedOutput(payload={"answer": "done", "score": 7})

    converted = convert_output(DemoOutput, extracted)

    assert converted == DemoOutput(answer="done", score=7)


def test_input_injection_requires_input_file_path_for_file_mode() -> None:
    with pytest.raises(InputInjectionError, match="input_file_path is required"):
        inject_input(
            InputMode.INPUT_FILE_JSON,
            DemoInput(name="alpha", count=3, metadata={"role": "admin"}, api_key="secret"),
        )


def test_output_extraction_rejects_invalid_json_stdout() -> None:
    with pytest.raises(OutputExtractionError, match="stdout is not valid JSON"):
        extract_output(OutputMode.JSON_STDOUT, stdout="not-json")


def test_output_extraction_rejects_missing_tagged_json_line() -> None:
    with pytest.raises(OutputExtractionError, match="no ZEROTH_OUTPUT_JSON line found"):
        extract_output(OutputMode.TAGGED_STDOUT_JSON, stdout="plain text\n")


def test_output_extraction_rejects_missing_output_file(tmp_path: Path) -> None:
    with pytest.raises(OutputExtractionError, match="output file does not exist"):
        extract_output(
            OutputMode.OUTPUT_FILE_JSON,
            stdout="ignored",
            output_file_path=tmp_path / "missing.json",
        )


def test_output_extraction_requires_exit_code_for_exit_code_mode() -> None:
    with pytest.raises(OutputExtractionError, match="exit_code is required"):
        extract_output(OutputMode.EXIT_CODE_ONLY, stdout="")


def test_output_conversion_rejects_invalid_payload() -> None:
    with pytest.raises(OutputConversionError):
        convert_output(DemoOutput, ExtractedOutput(payload={"answer": "done"}))
