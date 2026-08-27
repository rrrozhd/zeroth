from __future__ import annotations

from xml.etree import ElementTree

from zeroth.check.reporting.json_report import render_json
from zeroth.check.reporting.junit import render_junit
from zeroth.check.reporting.markdown import render_markdown
from zeroth.check.reporting.terminal import render_terminal
from zeroth.check.verdict.engine import reduce_verdict
from zeroth.check.verdict.models import (
    FaultSummary,
    OrdinarySummary,
    PrerequisiteSummary,
    UsageSummary,
)


def _verdict():
    return reduce_verdict(
        [],
        prerequisites=PrerequisiteSummary(valid=True, cases=1),
        ordinary=OrdinarySummary(runs=3, matches=3, required=2),
        faults=FaultSummary(required=4, executed=4, safety_violations=0),
        usage=UsageSummary(model_calls=0, complete=True),
    )


def test_four_renderers_share_one_immutable_verdict() -> None:
    verdict = _verdict()
    assert b'"status":"pass"' in render_json(verdict)
    assert render_markdown(verdict).startswith("# Zeroth Check: PASS")
    assert "\x1b" not in render_terminal(verdict)
    suite = ElementTree.fromstring(render_junit(verdict))
    assert suite.attrib == {"name": "zeroth-check", "tests": "5", "failures": "0", "skipped": "0"}
