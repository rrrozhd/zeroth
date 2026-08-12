"""Regression coverage for actionable documentation references."""

from __future__ import annotations

from pathlib import Path

import scripts.check_docs_references as docs_references
from scripts.check_docs_references import (
    Violation,
    _member_exists,
    document_paths,
    scan_markdown,
    unexpected_violations,
    valid_environment_variables,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORRECTIONS = REPO_ROOT / "scripts/docs_reference_corrections.txt"


def test_dead_actionable_references_are_reported() -> None:
    markdown = """\
```python
from zeroth.dead_module import Missing
```

```bash
export ZEROTH_DEAD_SETTING=true
```

Run `python src/zeroth/dead_script.py`.

Install with `pip install "zeroth-core[missing-extra]"`.
"""

    assert {
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/fixture.md", REPO_ROOT)
    } == {
        ("import", "zeroth.dead_module"),
        ("environment", "ZEROTH_DEAD_SETTING"),
        ("install-target", "zeroth-core[missing-extra]"),
        ("source-path", "src/zeroth/dead_script.py"),
    }


def test_missing_imported_member_is_reported() -> None:
    markdown = """\
```python
from zeroth.runtime.orchestration import DefinitelyMissing
```
"""

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/import.md", REPO_ROOT)
    ] == [("import-member", "zeroth.runtime.orchestration.DefinitelyMissing")]


def test_multiline_imported_members_are_resolved() -> None:
    markdown = """\
```python
from zeroth.runtime.orchestration import (
    RuntimeOrchestrator,
    DefinitelyMissing,
)
```
"""

    assert [
        (violation.line, violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/import.md", REPO_ROOT)
    ] == [(2, "import-member", "zeroth.runtime.orchestration.DefinitelyMissing")]


def test_member_resolution_does_not_import_the_target(tmp_path: Path) -> None:
    package = tmp_path / "src/zeroth/optional"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'raise RuntimeError("must not import")\n__all__ = ["Public"]\n',
        encoding="utf-8",
    )

    assert _member_exists("zeroth.optional", "Public", tmp_path)
    assert not _member_exists("zeroth.optional", "Missing", tmp_path)


def test_historical_examples_and_plain_prose_are_not_actionable() -> None:
    markdown = """\
The old example said from zeroth.dead_module import Missing and named
src/zeroth/dead_script.py, but this sentence is history rather than an instruction.

**Before:**

```python
from zeroth.dead_module import Missing
```

**After:**

```python
from zeroth.service.app import create_app
```
"""

    assert scan_markdown(markdown, "docs/history.md", REPO_ROOT) == []


def test_historical_inline_code_is_not_actionable() -> None:
    markdown = "The removed setting `ZEROTH_DEAD_SETTING` was used before 0.17."

    assert scan_markdown(markdown, "docs/history.md", REPO_ROOT) == []


def test_used_before_version_inline_code_is_not_actionable() -> None:
    markdown = "The setting `ZEROTH_DEAD_SETTING` was used before 0.17."

    assert scan_markdown(markdown, "docs/history.md", REPO_ROOT) == []


def test_inline_before_startup_remains_actionable() -> None:
    markdown = "Set `ZEROTH_DEAD_SETTING` before startup."

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/fixture.md", REPO_ROOT)
    ] == [("environment", "ZEROTH_DEAD_SETTING")]


def test_historical_inline_code_with_following_marker_is_not_actionable() -> None:
    markdown = "`ZEROTH_DEAD_SETTING` was removed."

    assert scan_markdown(markdown, "docs/history.md", REPO_ROOT) == []


def test_suffix_historical_marker_does_not_hide_replacement_target() -> None:
    markdown = "Use `ZEROTH_DEAD_NEW` instead of removed `ZEROTH_OLD`."

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/history.md", REPO_ROOT)
    ] == [("environment", "ZEROTH_DEAD_NEW")]


def test_parenthetical_historical_marker_does_not_hide_replacement_target() -> None:
    markdown = "Replace `ZEROTH_OLD` (removed) with `ZEROTH_DEAD_NEW`."

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/history.md", REPO_ROOT)
    ] == [("environment", "ZEROTH_DEAD_NEW")]


def test_historical_inline_does_not_hide_actionable_reference_on_same_line() -> None:
    markdown = (
        "The removed setting `ZEROTH_DEAD_SETTING` was used before 0.17; "
        "use `ZEROTH_CURRENTLY_DEAD_SETTING` now."
    )

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/history.md", REPO_ROOT)
    ] == [("environment", "ZEROTH_CURRENTLY_DEAD_SETTING")]


def test_historical_inline_same_clause_replacement_is_actionable() -> None:
    markdown = "Replace removed `ZEROTH_OLD` with `ZEROTH_DEAD_NEW`."

    assert [
        (violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/history.md", REPO_ROOT)
    ] == [("environment", "ZEROTH_DEAD_NEW")]


def test_only_settings_and_runtime_environment_access_are_valid() -> None:
    valid = valid_environment_variables(REPO_ROOT)

    assert "ZEROTH_OUTPUT_JSON" not in valid
    assert {
        "ZEROTH_ACCEPTANCE_ADMIN_KEY",
        "ZEROTH_ACCEPTANCE_OPERATOR_KEY",
        "ZEROTH_ACCEPTANCE_REVIEWER_KEY",
        "ZEROTH_CONSOLE_DIR",
        "ZEROTH_OUTPUT_FILE",
        "ZEROTH_SECRET__{}__{}",
        "ZEROTH_SERVICE_ROLES_JSON",
    } <= valid


def test_historical_migration_guide_is_scanned() -> None:
    assert REPO_ROOT / "docs/backend-import-migration.md" in document_paths(REPO_ROOT)


def test_multiline_install_target_is_reported() -> None:
    markdown = """\
```bash
pip install \\
  "zeroth-core[missing-extra]"
```
"""

    assert [
        (violation.line, violation.kind, violation.target)
        for violation in scan_markdown(markdown, "docs/install.md", REPO_ROOT)
    ] == [(2, "install-target", "zeroth-core[missing-extra]")]


def test_allowlist_accepts_removals_but_rejects_additions() -> None:
    allowed = {"docs/old.md:2:import:zeroth.dead_module"}
    old = Violation("docs/old.md", 2, "import", "zeroth.dead_module")
    new = Violation("docs/new.md", 4, "environment", "ZEROTH_NEW_SETTING")

    assert unexpected_violations([], allowed) == []
    assert unexpected_violations([old], allowed) == []
    assert unexpected_violations([old, new], allowed) == [new]


def test_allowlist_cannot_expand_beyond_seed(tmp_path: Path, monkeypatch, capsys) -> None:
    invalid_allowlist_entries = getattr(docs_references, "invalid_allowlist_entries", None)
    violation = Violation("docs/new.md", 4, "environment", "ZEROTH_NEW_SETTING")
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text(f"{violation.key}\n", encoding="utf-8")
    monkeypatch.setattr(docs_references, "ALLOWLIST", allowlist)
    monkeypatch.setattr(docs_references, "scan_repository", lambda: [violation])

    assert invalid_allowlist_entries is not None
    assert invalid_allowlist_entries({"known", "new"}, {"known"}) == {"new"}
    assert docs_references.main() == 1
    assert f"allowlist entry absent from baseline: {violation.key}" in capsys.readouterr().out


def test_baseline_seed_contains_the_16_reviewed_base_violations() -> None:
    assert len(docs_references.load_allowlist(docs_references.BASELINE)) == 16


def test_substituted_seed_cannot_authorize_new_violation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation = Violation("docs/new.md", 4, "environment", "ZEROTH_NEW_SETTING")
    seed = docs_references.BASELINE.read_text(encoding="utf-8")
    original = next(line for line in seed.splitlines() if line and not line.startswith("#"))
    baseline = tmp_path / "baseline.txt"
    allowlist = tmp_path / "allowlist.txt"
    baseline.write_text(seed.replace(original, violation.key, 1), encoding="utf-8")
    allowlist.write_text(f"{violation.key}\n", encoding="utf-8")
    monkeypatch.setattr(docs_references, "BASELINE", baseline)
    monkeypatch.setattr(docs_references, "ALLOWLIST", allowlist)
    monkeypatch.setattr(docs_references, "scan_repository", lambda: [violation])

    assert len(docs_references.load_allowlist(baseline)) == 16
    assert docs_references.main() == 1
    assert "documentation reference seed differs from reviewed baseline" in capsys.readouterr().out


def test_exact_19_reviewed_corrections_remain_current() -> None:
    project_version = docs_references.tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    seed_paths = [
        line.split(":", 1)[0]
        for line in docs_references.BASELINE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    corrections = [
        line.split("|", 2)
        for line in CORRECTIONS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert len(corrections) == 19
    assert [path for path, _, _ in corrections[:16]] == seed_paths
    assert [path for path, _, _ in corrections[16:]] == [
        "docs/concepts/econ.md",
        "docs/how-to/deployment/embedded-library.md",
        "docs/how-to/econ.md",
    ]
    for path, stale, current in corrections:
        text = " ".join((REPO_ROOT / path).read_text(encoding="utf-8").split())
        assert stale not in text, f"stale text remains in {path}: {stale}"
        current = current.format(project_version=project_version)
        assert current in text, f"current text missing from {path}: {current}"


def test_with_regulus_standalone_commands_are_protected() -> None:
    lines = (
        (REPO_ROOT / "docs/how-to/deployment/with-regulus.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert lines[81] == (
        "uv run uvicorn zeroth.econ.plane.main:app --port 8000   # the Regulus backend"
    )
    assert lines[93] == (
        "    image: zeroth-core:latest          # same image; runs zeroth.econ.plane.main:app"
    )
    assert lines[94] == (
        "    command: uvicorn zeroth.econ.plane.main:app --host 0.0.0.0 --port 8000"
    )


def test_migration_troubleshooting_names_only_retired_import_roots() -> None:
    guide = (REPO_ROOT / "docs/how-to/migration-from-monolith.md").read_text(encoding="utf-8")

    assert "`zeroth.<something>` (without `.core`)" not in guide
    assert "`zeroth.orchestrator`, `zeroth.graph`, `zeroth.memory`, or `zeroth.policy`" in guide
    assert "from zeroth.runtime.orchestration import RuntimeOrchestrator" in guide
    assert "from zeroth.integrations.memory import RunEphemeralMemoryConnector" in guide
