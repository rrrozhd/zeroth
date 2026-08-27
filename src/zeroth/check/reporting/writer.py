"""Atomic multi-format report writer."""

from __future__ import annotations

from pathlib import Path

from zeroth.check.reporting.json_report import render_json
from zeroth.check.reporting.junit import render_junit
from zeroth.check.reporting.markdown import render_markdown
from zeroth.check.reporting.terminal import render_terminal
from zeroth.check.tape.storage import atomic_write
from zeroth.check.verdict.models import CheckVerdict


def write_reports(
    verdict: CheckVerdict, directory: str | Path, *, fail_on_canary: bool = False
) -> dict[str, Path]:
    root = Path(directory).resolve()
    outputs = {
        "json": root / "check-verdict.json",
        "junit": root / "check-junit.xml",
        "markdown": root / "check-summary.md",
        "terminal": root / "check-terminal.txt",
    }
    atomic_write(outputs["json"], render_json(verdict), overwrite=True)
    atomic_write(
        outputs["junit"], render_junit(verdict, fail_on_canary=fail_on_canary), overwrite=True
    )
    atomic_write(outputs["markdown"], render_markdown(verdict).encode(), overwrite=True)
    atomic_write(outputs["terminal"], render_terminal(verdict).encode(), overwrite=True)
    return outputs
