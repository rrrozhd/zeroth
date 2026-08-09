"""Render the human-readable release verdict.

The machine-readable records decide the outcome; this renders the same
decision for the person who has to sign it off, naming every gate, what it
concluded, and -- when it blocked -- exactly which artifact was wrong. A
verdict that only says "failed" makes an operator go digging, which is when
gates get bypassed.
"""

from __future__ import annotations

from typing import Any

from gates.identity import identity_digest
from gates.validate import PASSED, GateResult, releasable

RELEASABLE = "RELEASABLE"
BLOCKED = "BLOCKED"

_SYMBOLS = {PASSED: "pass"}


def headline(results: list[GateResult]) -> str:
    """Return the one-word verdict."""
    return RELEASABLE if releasable(results) else BLOCKED


def render(
    manifest: dict[str, Any],
    candidate: dict[str, Any],
    results: list[GateResult],
    *,
    phase: str,
    trigger: str | None = None,
) -> str:
    """Return the verdict as Markdown."""
    verdict = headline(results)
    titles = {gate["id"]: gate["title"] for gate in manifest["gates"]}
    package = candidate.get("package", {})
    lines = [
        f"# Release verdict: {verdict}",
        "",
        f"- Phase: `{phase}`",
        f"- Trigger: `{trigger or 'all'}`",
        f"- Commit: `{candidate.get('commit', 'unknown')}`",
        f"- Version: `{package.get('version', 'unknown')}`",
        f"- Candidate identity: `{identity_digest(candidate)}`",
        "",
        "| # | Gate | Status | Detail |",
        "|---|---|---|---|",
    ]
    order = {gate["id"]: gate["order"] for gate in manifest["gates"]}
    for result in results:
        status = _SYMBOLS.get(result.status, result.status.upper())
        detail = result.reason.replace("|", "\\|")
        lines.append(
            f"| {order.get(result.gate, '-')} | {titles.get(result.gate, result.gate)} "
            f"| {status} | {detail} |"
        )
    lines.append("")
    if verdict == RELEASABLE:
        lines.append(
            f"All {len(results)} gates in the `{phase}` phase hold against this candidate."
        )
    else:
        blocking = [result for result in results if result.blocking]
        lines.append(
            f"{len(blocking)} of {len(results)} gates block promotion. "
            "Promotion stays closed until every one of them passes against this exact candidate."
        )
        lines.append("")
        for result in blocking:
            title = titles.get(result.gate, result.gate)
            lines.append(f"- **{title}** ({result.status}): {result.reason}")
    manual = sorted(
        name
        for name, kind in manifest["evidence_kinds"].items()
        if kind["applicable"] and kind.get("producer") == "manual"
    )
    if manual:
        lines.extend(
            [
                "",
                f"Operator-supplied evidence (no CI step produces it): {', '.join(manual)}.",
            ]
        )
    return "\n".join(lines) + "\n"
