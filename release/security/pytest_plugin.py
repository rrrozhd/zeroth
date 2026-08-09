"""Pytest evidence plugin for ZER-32 matrix-bound security tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from release.security.matrix import load_matrix


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("security evidence")
    group.addoption("--security-results", type=Path, help="atomic security outcome JSON path")
    group.addoption(
        "--security-matrix",
        type=Path,
        default=Path("release/security/security-matrix.json"),
        help="matrix whose bound nodes may not use xfail",
    )


def pytest_configure(config: pytest.Config) -> None:
    output = config.getoption("--security-results")
    if output is not None:
        config.stash[_RECORDS_KEY] = []
        _write_records(output, [])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    matrix_path = config.getoption("--security-matrix")
    if not matrix_path.exists():
        return
    bound = {nodeid for case in load_matrix(matrix_path).cases for nodeid in case.test_nodes}
    invalid = sorted(
        item.nodeid for item in items if item.nodeid in bound and item.get_closest_marker("xfail")
    )
    if invalid:
        raise pytest.UsageError(
            "matrix-bound security test may not use xfail: " + ", ".join(invalid)
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    output = item.config.getoption("--security-results")
    if output is None:
        return
    skip: str | None = None
    if report.skipped:
        longrepr = report.longrepr
        skip = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else "skipped"
    wasxfail = getattr(report, "wasxfail", None)
    record = {
        "nodeid": report.nodeid,
        "phase": report.when,
        "outcome": report.outcome,
        "skip": skip,
        "wasxfail": str(wasxfail) if wasxfail is not None else None,
    }
    records = item.config.stash[_RECORDS_KEY]
    records.append(record)
    _write_records(output, records)


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"schema_version": 1, "records": records},
                stream,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_RECORDS_KEY: pytest.StashKey[list[dict[str, object]]] = pytest.StashKey()
