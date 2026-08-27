"""JUnit XML mapping for Check ordinary and fault evidence."""

from __future__ import annotations

from xml.etree import ElementTree

from zeroth.check.verdict.models import CheckStatus, CheckVerdict


def render_junit(verdict: CheckVerdict, *, fail_on_canary: bool = False) -> bytes:
    total = 1 + verdict.faults.required
    should_fail = verdict.status in {CheckStatus.BLOCK, CheckStatus.INVALID} or (
        verdict.status is CheckStatus.CANARY and fail_on_canary
    )
    failures = total if should_fail else 0
    skipped = total if verdict.status is CheckStatus.CANARY and not fail_on_canary else 0
    suite = ElementTree.Element(
        "testsuite",
        name="zeroth-check",
        tests=str(total),
        failures=str(failures),
        skipped=str(skipped),
    )
    names = ["ordinary", *[f"fault-{index}" for index in range(1, verdict.faults.required + 1)]]
    reason_text = "\n".join(item.reason_code.value for item in verdict.reasons)
    for name in names:
        case = ElementTree.SubElement(suite, "testcase", classname="zeroth.check", name=name)
        if should_fail:
            ElementTree.SubElement(case, "failure", message=verdict.status.value).text = reason_text
        elif skipped:
            ElementTree.SubElement(case, "skipped", message="canary")
        ElementTree.SubElement(case, "system-out").text = reason_text
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
