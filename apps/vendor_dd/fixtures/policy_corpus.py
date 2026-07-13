"""The internal third-party-risk policy corpus.

Seeded into the shared memory connector at service bootstrap so the
``policy-context`` retrieval node has something real to ground on. In a
production deployment this would live in a vector store (Chroma, pgvector);
the connector interface is identical.
"""

POLICY_CORPUS: dict[str, str] = {
    "policy/tpr-001-data-classification": (
        "TPR-001 Data classification. Vendors receiving regulated personal data "
        "(PII, PHI, payroll, payment) are classified Tier A and require a full "
        "due-diligence panel, a signed DPA, and annual reassessment. Vendors with "
        "confidential business data are Tier B; internal-only integrations are Tier C."
    ),
    "policy/tpr-002-sanctions": (
        "TPR-002 Sanctions and watchlists. Every vendor and each of its named "
        "subprocessors must be screened against the consolidated sanctions "
        "denylist before onboarding. Any hit blocks auto-approval: the assessment "
        "must be routed to a human risk reviewer regardless of other scores."
    ),
    "policy/tpr-003-financial-viability": (
        "TPR-003 Financial viability. Vendors with annual spend above USD 250,000 "
        "require a financial review covering revenue trend, net margin, and "
        "current ratio. A current ratio below 1.0 or sustained negative margins "
        "raise a going-concern flag and elevate the risk tier."
    ),
    "policy/tpr-004-concentration": (
        "TPR-004 Concentration risk. Where a vendor is the sole provider of a "
        "business-critical capability, the assessment must record an exit plan "
        "and the report must include continuity conditions."
    ),
    "policy/tpr-005-approval-routing": (
        "TPR-005 Approval routing. Assessments scoring tier high or critical "
        "require sign-off by the third-party-risk reviewer before the report is "
        "released. Low and medium tiers may auto-release with conditions."
    ),
}
