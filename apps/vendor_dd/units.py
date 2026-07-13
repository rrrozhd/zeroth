"""Execution units of the vendor-dd workflow — all four onboarding modes.

* INLINE — ``NORMALIZE_SOURCE`` and ``RISK_SCORE_SOURCE`` travel inside the
  graph nodes themselves (Studio code-node path); nothing to register here.
* NATIVE — ``sanctions_screen_handler`` (tool-attached to the screening agent)
  and ``prepare_panel_handler`` (the fan-out source step).
* PROJECT — the ``finmetrics`` project tree under ``project_unit/`` with a
  real build step (CSV → JSON artifact).
* WRAPPED_COMMAND — ``report-stamp``: a shell + openssl pipeline that wraps
  the final report with its sha256 integrity fingerprint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.vendor_dd.contracts import (
    AssessmentReport,
    MetricsReport,
    PanelPrep,
    SanctionsQuery,
    SanctionsVerdict,
    ScreeningReport,
    StampedReport,
)
from zeroth.core.execution_units import (
    BuildConfig,
    CommandArtifactSource,
    ExecutableUnitRegistry,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    ProjectArchiveArtifactSource,
    ProjectUnitManifest,
    PythonModuleArtifactSource,
    RunConfig,
    WrappedCommandUnitManifest,
)

PROJECT_UNIT_DIR = Path(__file__).resolve().parent / "project_unit" / "finmetrics"

SANCTIONS_REF = "eu://vendor-dd/sanctions-screen"
PREPARE_PANEL_REF = "eu://vendor-dd/prepare-panel"
FINMETRICS_REF = "eu://vendor-dd/financial-metrics"
REPORT_STAMP_REF = "eu://vendor-dd/report-stamp"


# ---------------------------------------------------------------------------
# NATIVE: sanctions screening tool (attached to the screening agent by a
# tool-kind edge) and the panel-prep step.
# ---------------------------------------------------------------------------

SANCTIONS_DENYLIST: dict[str, str] = {
    "volkov digital services": "consolidated-denylist-2026",
    "crimson bridge analytics": "consolidated-denylist-2026",
    "aurora shell holdings": "consolidated-denylist-2026",
}


def sanctions_screen_handler(_ctx: Any, data: SanctionsQuery) -> dict[str, Any]:
    """Screen one legal name against the bundled denylist."""
    needle = data.name.strip().lower()
    for listed_name, list_name in SANCTIONS_DENYLIST.items():
        if listed_name in needle or needle in listed_name:
            return SanctionsVerdict(
                name=data.name, listed=True, list_name=list_name, match_score=1.0
            ).model_dump(mode="json")
    return SanctionsVerdict(name=data.name, listed=False).model_dump(mode="json")


DIMENSION_FOCUS = {
    "financial": "solvency, margin trend, going-concern indicators",
    "security": "data handling, subprocessor exposure, breach surface",
    "compliance": "sanctions exposure, jurisdiction, regulatory obligations",
}


def prepare_panel_handler(_ctx: Any, data: MetricsReport) -> dict[str, Any]:
    """Assemble one brief per risk dimension; the node fans out on ``panel``."""
    output = data.model_dump(mode="json")
    panel: list[dict[str, Any]] = []
    for dimension in data.dimensions:
        panel.append(
            {
                "dimension": dimension,
                "vendor_slug": data.vendor_slug,
                "vendor_name": data.vendor_name,
                "brief": (
                    f"Assess the {dimension} risk of {data.vendor_name} "
                    f"({data.category}, {data.jurisdiction}; spend band {data.spend_band}; "
                    f"data access {data.data_access}). Focus: "
                    f"{DIMENSION_FOCUS.get(dimension, 'general third-party risk')}."
                ),
                "flags": list(data.flags),
                "sanctions_status": data.sanctions_status,
                "data_access": data.data_access,
                "spend_band": data.spend_band,
                "financial_metrics": dict(data.financial_metrics),
                "policy_citations": list(data.policy_citations),
            }
        )
    output["panel"] = panel
    return output


# ---------------------------------------------------------------------------
# INLINE sources — authored code, content-addressed, sandboxed subprocess.
# ---------------------------------------------------------------------------

NORMALIZE_SOURCE = '''\
"""normalize: validate the mapped dossier and derive routing fields."""
import json
import re
import sys

payload = json.load(sys.stdin)

name = payload.get("vendor_name", "").strip()
if not name:
    raise SystemExit("vendor_name is required")

slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

spend = float(payload.get("annual_spend_usd") or 0.0)
if spend >= 1_000_000:
    band = "large"
elif spend >= 250_000:
    band = "medium"
elif spend >= 50_000:
    band = "small"
else:
    band = "micro"

data_risk = {"none": 0, "internal": 1, "confidential": 2, "regulated_pii": 3}.get(
    payload.get("data_access", "internal"), 1
)

out = dict(payload)
out.update(
    vendor_slug=slug,
    spend_band=band,
    data_risk=data_risk,
    query=(
        "third-party vendor policy for "
        + payload.get("category", "general")
        + " handling "
        + payload.get("data_access", "internal")
        + " data"
    ),
    dimensions=["financial", "security", "compliance"],
)
json.dump(out, sys.stdout)
'''

RISK_SCORE_SOURCE = '''\
"""risk-score: deterministic weighted scoring over the fan-in panel."""
import json
import sys

payload = json.load(sys.stdin)

findings = [f for f in payload.get("panel") or [] if isinstance(f, dict)]
ratings = [f.get("rating", 3) for f in findings] or [3]
panel_component = (sum(ratings) / len(ratings)) / 5.0 * 60.0

drivers = []
score = panel_component
worst = max(findings, key=lambda f: f.get("rating", 0), default=None)
if worst is not None:
    drivers.append(
        "worst dimension: " + worst.get("dimension", "?") + " rated " + str(worst.get("rating"))
    )

data_risk = int(payload.get("data_risk") or 0)
score += data_risk * 5.0
if data_risk >= 3:
    drivers.append("regulated personal data access")

if payload.get("sanctions_status") == "hit":
    score += 20.0
    drivers.append("sanctions denylist match (TPR-002: human review mandatory)")

metrics = payload.get("financial_metrics") or {}
if metrics.get("going_concern_flag"):
    score += 10.0
    drivers.append("going-concern flag from financial metrics")

score = round(min(score, 100.0), 1)
tier = "low" if score < 35 else "medium" if score < 55 else "high" if score < 75 else "critical"

out = dict(payload)
out.update(risk_score=score, tier=tier, drivers=drivers)
json.dump(out, sys.stdout)
'''


# ---------------------------------------------------------------------------
# WRAPPED_COMMAND: shell + openssl integrity stamp over the final report.
# ---------------------------------------------------------------------------

_STAMP_SCRIPT = (
    "payload=$(cat); "
    'digest=$(printf %s "$payload" | openssl dgst -sha256 -r | cut -d" " -f1); '
    'printf \'{"report": %s, "stamp": "sha256:%s"}\' "$payload" "$digest"'
)


def build_unit_registry() -> ExecutableUnitRegistry:
    """Register every manifest-backed unit (everything except the inline ones)."""
    registry = ExecutableUnitRegistry()

    registry.register(
        SANCTIONS_REF,
        NativeUnitManifest(
            unit_id="vendor-dd-sanctions-screen",
            artifact_source=PythonModuleArtifactSource(ref="apps.vendor_dd.units"),
            callable_ref="apps.vendor_dd.units:sanctions_screen_handler",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://vendor-dd/sanctions-query",
            output_contract_ref="contract://vendor-dd/sanctions-verdict",
        ),
        input_model=SanctionsQuery,
        output_model=SanctionsVerdict,
        handler=sanctions_screen_handler,
    )

    registry.register(
        PREPARE_PANEL_REF,
        NativeUnitManifest(
            unit_id="vendor-dd-prepare-panel",
            artifact_source=PythonModuleArtifactSource(ref="apps.vendor_dd.units"),
            callable_ref="apps.vendor_dd.units:prepare_panel_handler",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://vendor-dd/metrics",
            output_contract_ref="contract://vendor-dd/panel-prep",
        ),
        input_model=MetricsReport,
        output_model=PanelPrep,
        handler=prepare_panel_handler,
    )

    registry.register(
        FINMETRICS_REF,
        ProjectUnitManifest(
            unit_id="vendor-dd-financial-metrics",
            artifact_source=ProjectArchiveArtifactSource(ref=str(PROJECT_UNIT_DIR)),
            project_archive_ref=f"file://{PROJECT_UNIT_DIR}",
            build_config=BuildConfig(
                command=["python3", str(PROJECT_UNIT_DIR / "build_data.py")],
            ),
            run_config=RunConfig(command=["python3", "-I", str(PROJECT_UNIT_DIR / "main.py")]),
            input_mode=InputMode.INPUT_FILE_JSON,
            output_mode=OutputMode.OUTPUT_FILE_JSON,
            input_contract_ref="contract://vendor-dd/screening",
            output_contract_ref="contract://vendor-dd/metrics",
            cache_identity_fields={"project": "finmetrics", "rev": "1"},
        ),
        input_model=ScreeningReport,
        output_model=MetricsReport,
    )

    registry.register(
        REPORT_STAMP_REF,
        WrappedCommandUnitManifest(
            unit_id="vendor-dd-report-stamp",
            artifact_source=CommandArtifactSource(ref="openssl"),
            run_config=RunConfig(command=["/bin/sh", "-c", _STAMP_SCRIPT]),
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://vendor-dd/assessment",
            output_contract_ref="contract://vendor-dd/stamped",
        ),
        input_model=AssessmentReport,
        output_model=StampedReport,
    )

    return registry
