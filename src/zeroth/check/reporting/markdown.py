"""Human-readable Markdown verdict renderer."""

from __future__ import annotations

from zeroth.check.verdict.models import CheckVerdict


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(verdict: CheckVerdict) -> str:
    lines = [
        f"# Zeroth Check: {verdict.status.value.upper()}",
        "",
        f"Exit code: `{verdict.exit_code}`",
        "",
        "| Evidence | Observed | Required |",
        "|---|---:|---:|",
        f"| Ordinary matches | {verdict.ordinary.matches} | {verdict.ordinary.required} |",
        f"| Mandatory faults | {verdict.faults.executed} | {verdict.faults.required} |",
        f"| Safety violations | {verdict.faults.safety_violations} | 0 |",
        f"| Usage complete | {str(verdict.usage.complete).lower()} | true |",
        "",
        "## Reasons",
        "",
    ]
    if verdict.reasons:
        lines.extend(
            f"- `{item.reason_code.value}` ({_escape(item.scope_key)}): {_escape(item.summary)}"
            for item in verdict.reasons
        )
    else:
        lines.append("- All required evidence satisfied.")
    return "\n".join(lines) + "\n"
