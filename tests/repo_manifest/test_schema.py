"""ZER-37: the `.zeroth.yaml` v1 document contract.

Pure schema and policy checks -- no filesystem. The staged-checkout semantic
checks live in tests/repo_manifest/test_hostile_corpus.py. Throughout, hostile
text planted in the document must never surface in any issue message or path:
messages are rendered from codebase-owned templates, and unknown location
elements are redacted.
"""

from __future__ import annotations

import textwrap

import pytest

from zeroth.contracts.repo_manifest import (
    InputMode,
    NetworkAccess,
    OutputMode,
    RepoManifestDocument,
    RepoManifestValidationCode,
    RepoManifestValidationError,
    RepoManifestValidationReport,
    RepoRuntime,
    RepoUnitPolicy,
    evaluate_policy,
    parse_manifest_document,
)

CANARY = "31337_EVIL_CANARY_PAYLOAD"
REDACTED = "***REDACTED***"

MINIMAL = """\
schema_version: 1
scripts:
  train:
    entry: scripts/train.py
    runtime: python3
"""


def _parse(text: str) -> tuple[RepoManifestDocument | None, RepoManifestValidationReport]:
    document, report = parse_manifest_document(textwrap.dedent(text).encode())
    assert CANARY not in report.model_dump_json()
    return document, report


def _codes(report: RepoManifestValidationReport) -> list[RepoManifestValidationCode]:
    return [issue.code for issue in report.issues]


def test_full_document_parses_with_every_field() -> None:
    document, report = _parse(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            working_directory: "."
            input: {mode: json_stdin}
            output: {mode: json_stdout}
            environment: {LOG_LEVEL: info}
            resources: {cpu_cores: 1.0, memory_mb: 512, timeout_seconds: 120, max_processes: 16}
            network: {access: none}
            capabilities: [fs.read, "tool:search"]
            smoke:
              files_exist: [data/config.json]
              exit_code: 0
              stdout_contains: READY
        """
    )

    assert not report.issues
    assert document is not None
    script = document.scripts["train"]
    assert script.entry == "scripts/train.py"
    assert script.runtime is RepoRuntime.PYTHON3
    assert script.resources.memory_mb == 512
    assert script.network.access is NetworkAccess.NONE
    assert script.capabilities == ("fs.read", "tool:search")
    assert script.smoke is not None
    assert script.smoke.files_exist == ("data/config.json",)


def test_minimal_document_fills_documented_defaults() -> None:
    document, report = _parse(MINIMAL)

    assert not report.issues
    assert document is not None
    script = document.scripts["train"]
    assert script.working_directory == "."
    assert script.input.mode is InputMode.JSON_STDIN
    assert script.output.mode is OutputMode.JSON_STDOUT
    assert script.network.access is NetworkAccess.NONE
    assert script.environment == {}
    assert script.capabilities == ()
    assert script.resources.cpu_cores is None
    assert script.smoke is None


def test_unknown_key_is_rejected_and_redacted() -> None:
    document, report = _parse(
        f"""\
        schema_version: 1
        "{CANARY} <script>": 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
        """
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]
    assert REDACTED in report.issues[0].path


@pytest.mark.parametrize(
    ("version_line", "value_in_message"),
    [
        ("schema_version: 2", "2"),
        ("schema_version: \"1\"", None),
        ("schema_version: true", None),
        ("", None),
    ],
)
def test_unsupported_schema_versions(version_line: str, value_in_message: str | None) -> None:
    document, report = _parse(
        f"""\
        {version_line}
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
        """
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.SCHEMA_VERSION_UNSUPPORTED]
    message = report.issues[0].message
    if value_in_message is not None:
        assert value_in_message in message
    else:
        # The found value is surfaced only when it is an actual int.
        assert "true" not in message.lower() or "1" in message


def test_multiple_scripts_report_only_the_count() -> None:
    document, report = _parse(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
          evaluate:
            entry: scripts/evaluate.py
            runtime: python3
        """
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MULTIPLE_SCRIPTS_UNSUPPORTED]
    message = report.issues[0].message
    assert "2" in message
    assert "train" not in message
    assert "evaluate" not in message


def test_unsupported_runtime_gets_its_own_code() -> None:
    document, report = _parse(MINIMAL.replace("python3", "nodejs"))

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.UNSUPPORTED_RUNTIME]
    assert "python3" in report.issues[0].message
    assert "nodejs" not in report.issues[0].message


@pytest.mark.parametrize("mode", ["restricted", "wifi"])
def test_unsupported_network_mode_gets_its_own_code(mode: str) -> None:
    document, report = _parse(
        f"""\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            network: {{access: {mode}}}
        """
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.NETWORK_MODE_UNSUPPORTED]
    assert "wifi" not in report.issues[0].message


def test_restricted_network_mode_is_named_as_reserved() -> None:
    _document, report = _parse(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            network: {access: restricted}
        """
    )

    assert "restricted" in report.issues[0].message
    assert "reserved" in report.issues[0].message


@pytest.mark.parametrize(
    "entry",
    [
        "../outside.py",
        "/abs/path.py",
        "a\\\\b.py",
        "a/../b.py",
        "x/" * 33 + "f.py",
        "l" * 513,
        "a//b.py",
        "trailing/",
    ],
)
def test_out_of_bounds_entry_paths_are_shape_errors(entry: str) -> None:
    document, report = _parse(
        f"""\
        schema_version: 1
        scripts:
          train:
            entry: "{entry}"
            runtime: python3
        """
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]
    assert entry not in report.issues[0].message


def test_working_directory_may_be_dot_but_not_traversal() -> None:
    document, _report = _parse(MINIMAL)
    assert document is not None
    assert document.scripts["train"].working_directory == "."

    document, report = _parse(
        MINIMAL + "    working_directory: ../elsewhere\n",
    )
    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]


@pytest.mark.parametrize(
    "script_body",
    [
        "input: {mode: xml_stdin}",
        "output: {mode: yaml_stdout}",
        "environment: {lower_case: nope}",
        "environment: {LOG_LEVEL: \"" + "v" * 1025 + "\"}",
        "capabilities: [UPPER]",
        "capabilities: [" + ", ".join(f"c{i}" for i in range(17)) + "]",
        "resources: {cpu_cores: 0}",
        "resources: {memory_mb: -5}",
        "resources: {timeout_seconds: 0}",
        "resources: {memory_mb: \"512\"}",
        "smoke: {exit_code: 256}",
        "smoke: {stdout_contains: \"" + "s" * 257 + "\"}",
        "smoke: {files_exist: [" + ", ".join(f"f{i}" for i in range(17)) + "]}",
    ],
)
def test_field_bounds_are_enforced(script_body: str) -> None:
    document, report = _parse(MINIMAL + f"    {script_body}\n")

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]


def test_hostile_script_name_is_rejected_and_redacted() -> None:
    document, report = _parse(
        f"""\
        schema_version: 1
        scripts:
          "{CANARY} !":
            entry: scripts/train.py
            runtime: python3
        """
    )

    assert document is None
    assert RepoManifestValidationCode.MANIFEST_SHAPE_INVALID in _codes(report)
    for issue in report.issues:
        assert all(CANARY not in element for element in issue.path)


def test_report_raises_a_carrying_error() -> None:
    document, report = _parse(MINIMAL.replace("schema_version: 1", "schema_version: 7"))

    assert document is None
    assert report.has_errors
    with pytest.raises(RepoManifestValidationError) as excinfo:
        report.raise_for_errors()
    assert excinfo.value.report is report

    _document, clean = _parse(MINIMAL)
    assert not clean.has_errors
    clean.raise_for_errors()


def _document_with(script_body: str) -> RepoManifestDocument:
    document, report = _parse(MINIMAL + textwrap.indent(textwrap.dedent(script_body), "    "))
    assert not report.has_errors, report
    assert document is not None
    return document


def test_policy_permits_the_defaults() -> None:
    document = _document_with("resources: {cpu_cores: 1.0, memory_mb: 2048}\n")

    report = evaluate_policy(document, RepoUnitPolicy())

    assert not report.issues


@pytest.mark.parametrize(
    ("resources", "field_name"),
    [
        ("{cpu_cores: 2.0}", "cpu_cores"),
        ("{memory_mb: 4096}", "memory_mb"),
        ("{timeout_seconds: 1000}", "timeout_seconds"),
        ("{max_processes: 128}", "max_processes"),
    ],
)
def test_resources_above_the_policy_ceiling_are_refused(resources: str, field_name: str) -> None:
    document = _document_with(f"resources: {resources}\n")

    report = evaluate_policy(document, RepoUnitPolicy())

    assert _codes(report) == [RepoManifestValidationCode.RESOURCE_LIMIT_ABOVE_CEILING]
    assert field_name in report.issues[0].message


def test_network_access_is_denied_by_default_policy() -> None:
    document = _document_with("network: {access: full}\n")

    report = evaluate_policy(document, RepoUnitPolicy())

    assert _codes(report) == [RepoManifestValidationCode.NETWORK_ACCESS_DENIED_BY_POLICY]

    permissive = evaluate_policy(document, RepoUnitPolicy(allow_network=True))
    assert not permissive.issues


@pytest.mark.parametrize("key", ["PATH", "ZEROTH_SECRET", "PYTHONPATH", "PYTHON"])
def test_reserved_environment_keys_are_refused(key: str) -> None:
    document = _document_with(f"environment: {{{key}: value}}\n")

    report = evaluate_policy(document, RepoUnitPolicy())

    assert _codes(report) == [RepoManifestValidationCode.ENVIRONMENT_KEY_RESERVED]
    assert report.issues[0].path[-1] == key


def test_unreserved_environment_keys_pass_policy() -> None:
    document = _document_with("environment: {LOG_LEVEL: info, PY_COLOR: '1'}\n")

    report = evaluate_policy(document, RepoUnitPolicy())

    assert not report.issues
