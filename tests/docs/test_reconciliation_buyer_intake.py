from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "economic-diagnostic-pilot.yml"
README = ROOT / "README.md"
PILOT = ROOT / "docs" / "operations" / "economic-debugger-commercial-pilot.md"


def _form() -> dict:
    return yaml.safe_load(FORM.read_text(encoding="utf-8"))


def _fields() -> dict[str, dict]:
    return {item["id"]: item for item in _form()["body"] if isinstance(item, dict) and "id" in item}


def test_public_intake_measures_one_provider_bill_closure_hypothesis() -> None:
    form = _form()
    fields = _fields()

    assert form["name"] == "Provider bill reconciliation design-partner request"
    assert set(fields) == {
        "monthly_provider_spend",
        "current_process",
        "provider_export",
        "closure_gap",
        "outcome_coverage",
        "deployment",
        "monthly_budget",
        "artifact_stage",
        "discovery_source",
        "operating_problem",
        "privacy",
    }
    assert all(field.get("validations", {}).get("required") is True for field in fields.values())

    export_options = fields["provider_export"]["attributes"]["options"]
    assert "OpenAI Costs API or export" in export_options
    assert "Anthropic cost and usage export" in export_options
    assert "No export available yet" in export_options

    closure_options = fields["closure_gap"]["attributes"]["options"]
    assert closure_options == [
        "Provider total does not close to measured telemetry",
        "Spend cannot be allocated to an owning team, customer, or workflow",
        "Spend cannot be tied to successful versus failed outcomes",
        "Finance or compliance does not trust the available evidence",
        "No current provider-bill closure problem",
    ]

    assert fields["artifact_stage"]["attributes"]["options"] == [
        "Generated a real provider-bill closure report",
        "Generated a real economic diagnostic only",
        "Ran the synthetic demo only",
        "Have not run Zeroth yet",
    ]
    assert fields["discovery_source"]["attributes"]["options"] == [
        "Hacker News",
        "GitHub",
        "PyPI",
        "Search",
        "Colleague or community referral",
        "Other",
    ]


def test_public_intake_forbids_sensitive_financial_and_runtime_evidence() -> None:
    notice = _form()["body"][0]["attributes"]["value"].lower()

    for prohibited in (
        "invoice",
        "provider credentials",
        "prompts",
        "responses",
        "traces",
        "customer or user identifiers",
        "unredacted",
    ):
        assert prohibited in notice

    privacy_options = _fields()["privacy"]["attributes"]["options"]
    assert privacy_options == [
        {
            "label": "I confirm this issue contains only non-sensitive aggregate information.",
            "required": True,
        }
    ]


def test_readme_first_screen_leads_with_economic_change_control_and_activation() -> None:
    first_screen = README.read_text(encoding="utf-8")[:5000]
    prose = " ".join(first_screen.split())

    assert "Test AI cost cuts before production" in prose
    assert "measured cost per accepted outcome" in prose
    assert "find → simulate → approve → verify" in prose
    assert "workflow-version decisions" in prose
    assert "pip install zeroth-sdk" in prose
    assert "release-blocked on hosted operations and package release readiness" in prose.lower()
    assert "hosted backtest execution" in prose
    assert first_screen.index("Test AI cost cuts before production") < first_screen.index(
        "Preserved platform capabilities"
    )
    assert "Close the AI spend ledger" not in first_screen


def test_synthetic_demo_is_not_counted_as_commercial_activation() -> None:
    pilot = PILOT.read_text(encoding="utf-8")

    assert "zeroth-econ demo" in pilot
    assert "does not count as a first diagnostic" in pilot
    assert "demo → real instrumentation" in pilot
