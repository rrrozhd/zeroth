"""ZER-37: the full pipeline against a hostile repository checkout.

Every test drives ``parse_manifest_document`` and (when a document survives)
``validate_staged_manifest`` against real fixture trees built in ``tmp_path``:
traversal entries, symlinked entries and working directories, missing files,
and the parser corpus (alias bomb, deep nesting, multi-document streams,
duplicate keys, oversized documents, non-mapping roots). Canary substrings are
planted in filenames, keys, and values; one sweeping helper serializes every
produced report and asserts the canaries appear nowhere.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from zeroth.contracts.repo_manifest import (
    RepoManifestDocument,
    RepoManifestValidationCode,
    RepoManifestValidationReport,
    parse_manifest_document,
)
from zeroth.integrations.execution.repo_units import validate_staged_manifest

CANARY = "31337_EVIL_CANARY_PAYLOAD"


def _assert_sanitized(report: RepoManifestValidationReport) -> None:
    """The one sweeping assertion: no canary in any rendering of the report."""
    for rendered in (str(report), report.model_dump_json(), repr(report)):
        assert CANARY not in rendered


def _run_pipeline(
    manifest: str, staged_root: Path
) -> tuple[RepoManifestDocument | None, RepoManifestValidationReport]:
    """Parse, then -- when a document survives -- validate against the checkout."""
    document, report = parse_manifest_document(textwrap.dedent(manifest).encode())
    _assert_sanitized(report)
    if document is None:
        return None, report
    staged_report = validate_staged_manifest(document, staged_root)
    _assert_sanitized(staged_report)
    return document, staged_report


def _manifest(entry: str = "scripts/train.py", body: str = "") -> str:
    return (
        "schema_version: 1\n"
        "scripts:\n"
        "  train:\n"
        f'    entry: "{entry}"\n'
        "    runtime: python3\n" + textwrap.indent(textwrap.dedent(body), "    ")
    )


@pytest.fixture
def staged_root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "train.py").write_text("print('ok')\n")
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text("{}\n")
    return root


def _codes(report: RepoManifestValidationReport) -> list[RepoManifestValidationCode]:
    return [issue.code for issue in report.issues]


def test_clean_checkout_passes_the_whole_pipeline(staged_root: Path) -> None:
    document, report = _run_pipeline(
        _manifest(body="smoke: {files_exist: [data/config.json]}\n"),
        staged_root,
    )

    assert document is not None
    assert not report.issues


@pytest.mark.parametrize("entry", [f"../{CANARY}.py", f"/tmp/{CANARY}.py"])
def test_traversal_entries_die_at_parse(entry: str, staged_root: Path) -> None:
    document, report = _run_pipeline(_manifest(entry=entry), staged_root)

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]


def test_symlinked_directory_escaping_the_checkout_is_refused(
    staged_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tool.py").write_text("print('outside')\n")
    (staged_root / "vendor").symlink_to(outside)

    _document, report = _run_pipeline(_manifest(entry="vendor/tool.py"), staged_root)

    assert _codes(report) == [RepoManifestValidationCode.SCRIPT_PATH_ESCAPES_CHECKOUT]


def test_symlinked_entry_file_is_refused_even_inside_the_checkout(staged_root: Path) -> None:
    (staged_root / "scripts" / "alias.py").symlink_to(staged_root / "scripts" / "train.py")

    _document, report = _run_pipeline(_manifest(entry="scripts/alias.py"), staged_root)

    assert _codes(report) == [RepoManifestValidationCode.SCRIPT_NOT_A_FILE]


def test_missing_entry_is_refused_without_echoing_its_name(staged_root: Path) -> None:
    _document, report = _run_pipeline(_manifest(entry=f"scripts/{CANARY}.py"), staged_root)

    assert _codes(report) == [RepoManifestValidationCode.SCRIPT_NOT_A_FILE]


def test_directory_entry_is_not_a_file(staged_root: Path) -> None:
    _document, report = _run_pipeline(_manifest(entry="scripts"), staged_root)

    assert _codes(report) == [RepoManifestValidationCode.SCRIPT_NOT_A_FILE]


def test_symlinked_workdir_escaping_the_checkout_is_refused(
    staged_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (staged_root / "wd").symlink_to(outside)

    _document, report = _run_pipeline(
        _manifest(body="working_directory: wd\n"), staged_root
    )

    assert _codes(report) == [RepoManifestValidationCode.WORKDIR_ESCAPES_CHECKOUT]


@pytest.mark.parametrize("workdir", ["scripts/train.py", f"{CANARY.lower()}_missing"])
def test_workdir_must_be_an_existing_directory(workdir: str, staged_root: Path) -> None:
    _document, report = _run_pipeline(
        _manifest(body=f'working_directory: "{workdir}"\n'), staged_root
    )

    assert _codes(report) == [RepoManifestValidationCode.WORKDIR_NOT_A_DIRECTORY]


def test_workdir_traversal_dies_at_parse(staged_root: Path) -> None:
    document, report = _run_pipeline(
        _manifest(body="working_directory: ../elsewhere\n"), staged_root
    )

    assert document is None
    assert _codes(report) == [RepoManifestValidationCode.MANIFEST_SHAPE_INVALID]


def test_smoke_path_escaping_via_symlink_is_refused(staged_root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text("{}\n")
    (staged_root / "out.json").symlink_to(secret)

    _document, report = _run_pipeline(
        _manifest(body="smoke: {files_exist: [out.json]}\n"), staged_root
    )

    assert _codes(report) == [RepoManifestValidationCode.SMOKE_PATH_ESCAPES_CHECKOUT]


def test_missing_smoke_file_is_refused_without_echoing_its_name(staged_root: Path) -> None:
    _document, report = _run_pipeline(
        _manifest(body=f"smoke: {{files_exist: [data/{CANARY}.json]}}\n"), staged_root
    )

    assert _codes(report) == [RepoManifestValidationCode.SMOKE_FILE_MISSING]
    assert report.issues[0].path[-1] == "0"


def test_multiple_staged_defects_are_all_reported(staged_root: Path) -> None:
    _document, report = _run_pipeline(
        _manifest(
            entry="scripts/gone.py",
            body=(
                "working_directory: nowhere\n"
                f"smoke: {{files_exist: [data/config.json, {CANARY.lower()}.json]}}\n"
            ),
        ),
        staged_root,
    )

    assert _codes(report) == [
        RepoManifestValidationCode.SCRIPT_NOT_A_FILE,
        RepoManifestValidationCode.WORKDIR_NOT_A_DIRECTORY,
        RepoManifestValidationCode.SMOKE_FILE_MISSING,
    ]


# --- Parser corpus: hostile bytes that must die before any document exists ---


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            f"a: &{CANARY} [lol, lol]\nb: [*{CANARY}, *{CANARY}]\n",
            RepoManifestValidationCode.YAML_ALIAS_FORBIDDEN,
        ),
        (
            "key: " + "[" * 30 + "]" * 30 + "\n",
            RepoManifestValidationCode.YAML_TOO_DEEP,
        ),
        (
            f"---\nschema_version: 1\n---\n{CANARY}: 1\n",
            RepoManifestValidationCode.YAML_MULTIDOC_FORBIDDEN,
        ),
        (
            f"{CANARY}: 1\n{CANARY}: 2\n",
            RepoManifestValidationCode.YAML_DUPLICATE_KEY,
        ),
        (
            "# " + "x" * 131_100 + "\n",
            RepoManifestValidationCode.YAML_TOO_LARGE,
        ),
        (
            f"- {CANARY}\n- 2\n",
            RepoManifestValidationCode.YAML_ROOT_NOT_MAPPING,
        ),
        (
            f"{CANARY}: [{CANARY}\n",
            RepoManifestValidationCode.YAML_PARSE_ERROR,
        ),
    ],
)
def test_parser_corpus_is_refused_with_stable_codes(
    payload: str, code: RepoManifestValidationCode, staged_root: Path
) -> None:
    document, report = _run_pipeline(payload, staged_root)

    assert document is None
    assert _codes(report) == [code]
