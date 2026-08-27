"""Safety-gated live Studio evaluation harness."""

from .action_runner import (
    EVALUATION_ACTION_MANIFEST_REF,
    EvaluationActionPayload,
    EvaluationActionRunner,
)
from .action_sink import (
    ActionPayloadConflictError,
    ActionReceipt,
    ActionSinkUnavailableError,
    EvaluationActionSink,
)
from .bootstrap import seed_campaign_bootstrap
from .campaign_finalizer import EvidenceFirstCampaignFinalizer
from .config import CampaignConfig, ResolvedPaidCampaign
from .control_gate import (
    BudgetGateProof,
    ChromaCorpusDocument,
    ChromaIdentity,
    ControlPlaneGate,
    PaidProbeAuthorization,
    PaidProbeExecutor,
    PaidProbeResult,
    ProbeAlreadyAuthorizedError,
    SignedAuditReadiness,
)
from .control_gate_runtime import (
    ChromaInspector,
    ChromaSeeder,
    ControlGateRuntime,
    LocalGateResult,
    SignedAuditInspector,
    SyntheticChromaDocument,
)
from .control_plane import (
    ControlPlaneEvidence,
    dirty_tree_hash,
    initialize_control_plane_evidence,
)
from .criteria import original_acceptance_criteria
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore, UnsafeEvidenceError
from .ledger import CampaignHaltedError, CampaignLedger
from .reconciliation import (
    ActionReceiptRecord,
    AuditRecord,
    Discrepancy,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    ReconciliationResult,
    RegulusExecutionEvent,
    ReservationRecord,
    reconcile_campaign,
)
from .runner import (
    CommandResult,
    CommandSpec,
    EvaluationReport,
    baseline_commands,
    execute_commands,
)

__all__ = [
    "ActionReceiptRecord",
    "AuditRecord",
    "CampaignConfig",
    "CampaignHaltedError",
    "CampaignLedger",
    "BudgetGateProof",
    "AcceptanceCriterion",
    "ActionPayloadConflictError",
    "ActionReceipt",
    "ActionSinkUnavailableError",
    "CommandResult",
    "CommandSpec",
    "CorrelationIds",
    "ControlPlaneEvidence",
    "ControlPlaneGate",
    "ControlGateRuntime",
    "ChromaCorpusDocument",
    "ChromaIdentity",
    "ChromaInspector",
    "ChromaSeeder",
    "Discrepancy",
    "EvaluationReport",
    "EvidenceFirstCampaignFinalizer",
    "EvaluationActionSink",
    "EvaluationActionPayload",
    "EvaluationActionRunner",
    "EVALUATION_ACTION_MANIFEST_REF",
    "EvidenceStore",
    "LocalCostEvent",
    "ProviderWindowSummary",
    "PaidProbeAuthorization",
    "PaidProbeExecutor",
    "PaidProbeResult",
    "ProbeAlreadyAuthorizedError",
    "ReconciliationInput",
    "ReconciliationResult",
    "RegulusExecutionEvent",
    "ReservationRecord",
    "ResolvedPaidCampaign",
    "SignedAuditReadiness",
    "SignedAuditInspector",
    "LocalGateResult",
    "SyntheticChromaDocument",
    "UnsafeEvidenceError",
    "baseline_commands",
    "dirty_tree_hash",
    "execute_commands",
    "initialize_control_plane_evidence",
    "original_acceptance_criteria",
    "reconcile_campaign",
    "seed_campaign_bootstrap",
]
