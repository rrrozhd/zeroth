"""Contracts for the vendor due-diligence workflow.

Every node boundary in the three graphs is typed by one of these models,
registered under ``contract://vendor-dd/*``. Intermediate contracts use
``extra="allow"`` deliberately: deterministic units echo their input and add
fields, so the payload is a growing context dict; the contract names the
fields a stage *requires*, not everything it carries.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VendorDossier(BaseModel):
    """Public input contract: what a requester submits about a vendor."""

    model_config = ConfigDict(extra="allow")

    vendor_name: str
    website: str = ""
    description: str
    jurisdiction: str
    category: str
    annual_spend_usd: float = 0.0
    data_access: str = "internal"  # none | internal | confidential | regulated_pii
    subprocessors: list[str] = Field(default_factory=list)


class MappedDossier(BaseModel):
    """Dossier after the intake edge mapping (rename/constant/default applied)."""

    model_config = ConfigDict(extra="allow")

    vendor_name: str
    vendor_website: str = ""
    description: str
    jurisdiction: str
    category: str
    annual_spend_usd: float = 0.0
    data_access: str = "internal"
    subprocessors: list[str] = Field(default_factory=list)
    pipeline: str = "vendor-dd/v1"
    priority: str = "standard"


class NormalizedDossier(MappedDossier):
    """Dossier enriched by the inline normalize unit."""

    vendor_slug: str
    spend_band: str
    data_risk: int
    query: str
    dimensions: list[str]


class GroundedDossier(NormalizedDossier):
    """Normalized dossier plus retrieved policy context (RAG output)."""

    policy_context: list[dict[str, Any]] = Field(default_factory=list)


class SanctionsQuery(BaseModel):
    """Input of the sanctions screening tool."""

    name: str


class SanctionsVerdict(BaseModel):
    """Output of the sanctions screening tool."""

    name: str
    listed: bool
    list_name: str | None = None
    match_score: float = 0.0


class ScreeningReport(BaseModel):
    """The screening analyst's structured verdict.

    Echo fields (slug, jurisdiction, spend, dimensions, …) are part of the
    schema on purpose: the agent is the narrowing point of the pipeline, and
    downstream deterministic stages need these identifiers.
    """

    model_config = ConfigDict(extra="allow")

    vendor_slug: str
    vendor_name: str
    jurisdiction: str
    category: str
    annual_spend_usd: float
    data_access: str
    data_risk: int
    spend_band: str
    dimensions: list[str]
    summary: str
    flags: list[str] = Field(default_factory=list)
    sanctions_status: str = "clear"  # clear | hit
    policy_citations: list[str] = Field(default_factory=list)


class MetricsReport(ScreeningReport):
    """Screening report plus financial metrics from the project unit."""

    financial_metrics: dict[str, Any] = Field(default_factory=dict)
    financials_found: bool = False


class PanelPrep(MetricsReport):
    """Metrics report plus the per-dimension briefs to fan out on."""

    panel: list[dict[str, Any]]


class PanelBranch(BaseModel):
    """One dimension brief — the child graph's public input contract."""

    model_config = ConfigDict(extra="allow")

    dimension: str
    vendor_slug: str
    vendor_name: str
    brief: str
    flags: list[str] = Field(default_factory=list)
    sanctions_status: str = "clear"
    data_access: str = "internal"
    spend_band: str = "small"
    financial_metrics: dict[str, Any] = Field(default_factory=dict)
    policy_citations: list[str] = Field(default_factory=list)


class DimensionFinding(BaseModel):
    """One dimension analyst's verdict — the child graph's output contract."""

    model_config = ConfigDict(extra="allow")

    dimension: str
    vendor_slug: str
    rating: int = Field(ge=1, le=5)  # 5 = severe risk
    rationale: str
    red_flags: list[str] = Field(default_factory=list)


class PanelFindings(MetricsReport):
    """Fan-in output: the panel list now holds DimensionFinding dicts."""

    panel: list[dict[str, Any] | None]


class RiskDecision(BaseModel):
    """Deterministic scoring output of the inline risk-score unit."""

    model_config = ConfigDict(extra="allow")

    vendor_slug: str
    vendor_name: str
    risk_score: float
    tier: str  # low | medium | high | critical
    drivers: list[str] = Field(default_factory=list)
    panel: list[dict[str, Any] | None] = Field(default_factory=list)
    sanctions_status: str = "clear"
    data_access: str = "internal"
    spend_band: str = "small"
    summary: str = ""


class DimensionSummary(BaseModel):
    """One panel finding, summarized in the final memo."""

    dimension: str
    rating: int = Field(ge=1, le=5)
    note: str


class AssessmentReport(BaseModel):
    """The report writer's final memo."""

    model_config = ConfigDict(extra="allow")

    vendor_name: str
    vendor_slug: str
    tier: str
    risk_score: float
    decision: str  # approved | conditional | rejected
    executive_summary: str
    dimension_summaries: list[DimensionSummary] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)


class StampedReport(BaseModel):
    """Final output: the report wrapped with its integrity fingerprint."""

    model_config = ConfigDict(extra="allow")

    report: dict[str, Any]
    stamp: str


class ChatTurn(BaseModel):
    """Input of the follow-up chat deployment (message-list agent)."""

    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    vendor: str = ""


class ChatReply(BaseModel):
    """Output of the follow-up chat deployment."""

    model_config = ConfigDict(extra="allow")

    reply: str


CONTRACTS: dict[str, type[BaseModel]] = {
    "contract://vendor-dd/dossier": VendorDossier,
    "contract://vendor-dd/mapped-dossier": MappedDossier,
    "contract://vendor-dd/normalized": NormalizedDossier,
    "contract://vendor-dd/grounded": GroundedDossier,
    "contract://vendor-dd/sanctions-query": SanctionsQuery,
    "contract://vendor-dd/sanctions-verdict": SanctionsVerdict,
    "contract://vendor-dd/screening": ScreeningReport,
    "contract://vendor-dd/metrics": MetricsReport,
    "contract://vendor-dd/panel-prep": PanelPrep,
    "contract://vendor-dd/panel-branch": PanelBranch,
    "contract://vendor-dd/dimension-finding": DimensionFinding,
    "contract://vendor-dd/panel-findings": PanelFindings,
    "contract://vendor-dd/risk-decision": RiskDecision,
    "contract://vendor-dd/assessment": AssessmentReport,
    "contract://vendor-dd/stamped": StampedReport,
    "contract://vendor-dd/chat-turn": ChatTurn,
    "contract://vendor-dd/chat-reply": ChatReply,
}
