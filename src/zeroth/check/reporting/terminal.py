"""Plain terminal projection with no ANSI sequences."""

from __future__ import annotations

from zeroth.check.verdict.models import CheckVerdict


def render_terminal(verdict: CheckVerdict) -> str:
    lines = [
        f"Zeroth Check: {verdict.status.value.upper()} (exit {verdict.exit_code})",
        (
            f"ordinary: {verdict.ordinary.matches}/{verdict.ordinary.runs} matched; "
            f"required {verdict.ordinary.required}"
        ),
        (
            f"faults: {verdict.faults.executed}/{verdict.faults.required} executed; "
            f"{verdict.faults.safety_violations} safety violations"
        ),
        f"usage: {'complete' if verdict.usage.complete else 'incomplete'}",
    ]
    lines.extend(
        f"- {item.reason_code.value} [{item.scope_key}]: {item.summary}" for item in verdict.reasons
    )
    return "\n".join(lines) + "\n"
