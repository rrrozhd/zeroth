"""Path-independent coverage guard for service schema models during Task 10.

``_discover_schema_models`` in :mod:`tests.architecture.test_library_surface`
selects schema-bearing modules by *directory name*: a file counts only when its
parent directory is literally ``service``. Relocating an API module to
``zeroth/service/api/`` therefore removes it from discovery, and because the
reverse-coverage assertion is a ``discovered - canonical`` subset check, the
models drop out **silently** rather than failing.

Extending that predicate to the new layout cannot be done while the move is in
flight: the moved models would then be discovered under their new module path
while the canonical fixture still records the old one, so the production move
commit would fail its own hook unless it also edited the golden fixture -- which
the refactor's commit-hygiene rule forbids.

This guard closes that window. It pins the service schema models by
``<module stem>:<model>`` instead of by full module path, so the set is
invariant under relocation: a model that is moved still matches, but a model
that is accidentally dropped, renamed, or left behind does not.

Remove this module in the final Task 10 commit, once every service API module
has been relocated and repointed, and ``_discover_schema_models`` has been
extended to cover ``zeroth/service/api/``.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"

_SCHEMA_BEARING_NAMES = {"app.py", "health.py", "studio_schemas.py"}

# Service schema models as of the start of Task 10, keyed by module stem so the
# set is stable across relocation. Regenerating this list to make a failure go
# away defeats the guard: a diff here means a model was added, dropped, or
# renamed, and that is exactly what needs review.
EXPECTED_SERVICE_SCHEMA_MODELS = {
    "admin_api:AdminRunListResponse",
    "approval_api:ApprovalResolutionRequest",
    "approval_api:ApprovalResolutionResponse",
    "audit_api:AttestationVerificationResponse",
    "audit_api:AuditRecordListResponse",
    "audit_api:AuditTimelineResponse",
    "audit_api:AuditVerificationResponse",
    "audit_api:DeploymentAttestationResponse",
    "audit_api:DeploymentEvidenceResponse",
    "audit_api:EvidenceSummaryResponse",
    "audit_api:RunEvidenceResponse",
    "audit_api:VerifyChainRequest",
    "connector_api:ConnectorCreateRequest",
    "connector_api:ConnectorSummaryResponse",
    "connector_api:ConnectorTestResponse",
    "connector_api:ConnectorUpdateRequest",
    "contracts_api:DeploymentResultErrorStateSchemaResponse",
    "contracts_api:DeploymentVersionMetadataResponse",
    "contracts_api:PublicContractSchemaResponse",
    "cost_api:DeploymentCostResponse",
    "cost_api:TenantBudgetRequest",
    "cost_api:TenantCostResponse",
    "deployment_api:CreateDeploymentRequest",
    "deployment_api:DeploymentSummaryResponse",
    "deployment_api:RollbackDeploymentRequest",
    "econ_analytics_api:QualityVerdictRequest",
    # HealthResponse's stem changed app->health in the health move: defining it
    # in zeroth/service/app.py would put it back into path-sensitive discovery
    # under a new module path and deadlock the app move. Reviewed, deliberate.
    "health:HealthResponse",
    "health:DependencyStatus",
    "health:LivenessResponse",
    "health:ReadinessResponse",
    "manifest_api:ManifestSummaryResponse",
    "retention_api:ErasureRequestBody",
    "retention_api:ErasureResponse",
    "retention_api:ErasureRunResult",
    "retention_api:LegalHoldBody",
    "retention_api:LegalHoldResponse",
    "retention_api:RetentionPolicyBody",
    "retention_api:RetentionPolicyResponse",
    "rightsizing_api:ExperimentRequest",
    "rightsizing_api:RightsizingRequest",
    "run_api:ApprovalPausedState",
    "run_api:RunInvocationRequest",
    "run_api:RunInvocationResponse",
    "run_api:RunStatusResponse",
    "studio_schemas:CreateContractRequest",
    "studio_schemas:CreateWorkflowRequest",
    "studio_schemas:NodeTypeResponse",
    "studio_schemas:PortDefinitionResponse",
    "studio_schemas:StudioContractResponse",
    "studio_schemas:StudioEdgeResponse",
    "studio_schemas:StudioNodeResponse",
    "studio_schemas:StudioPosition",
    "studio_schemas:StudioViewport",
    "studio_schemas:UpdateWorkflowRequest",
    "studio_schemas:WorkflowDetailResponse",
    "studio_schemas:WorkflowSummaryResponse",
    "template_api:CreateTemplateRequest",
    "template_api:TemplateListResponse",
    "template_api:TemplateResponse",
    "webhook_api:CreateSubscriptionRequest",
    "webhook_api:WebhookDeadLetterListResponse",
    "webhook_api:WebhookDeadLetterResponse",
    "webhook_api:WebhookSubscriptionListResponse",
    "webhook_api:WebhookSubscriptionResponse",
}


def _service_schema_modules() -> set[Path]:
    """Return schema-bearing service modules in the legacy or canonical tree."""
    roots = (
        SOURCE_ROOT / "zeroth" / "core" / "service",
        SOURCE_ROOT / "zeroth" / "service",
    )
    return {
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.py")
        if path.name.endswith("_api.py") or path.name in _SCHEMA_BEARING_NAMES
    }


def discover_service_schema_models() -> set[str]:
    """Collect ``<module stem>:<model>`` for every service schema model."""
    discovered: set[str] = set()
    for path in _service_schema_modules():
        module_name = ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module_name
                and issubclass(value, BaseModel)
            ):
                discovered.add(f"{path.stem}:{name}")
    return discovered


def test_no_service_schema_model_is_lost_while_relocating() -> None:
    """Relocation must preserve every service schema model, wherever it lands."""
    discovered = discover_service_schema_models()

    dropped = sorted(EXPECTED_SERVICE_SCHEMA_MODELS - discovered)
    assert not dropped, f"service schema models lost during relocation: {dropped}"

    added = sorted(discovered - EXPECTED_SERVICE_SCHEMA_MODELS)
    assert not added, f"unrecorded service schema models: {added}"


def test_each_service_schema_model_is_defined_exactly_once() -> None:
    """A model must not be defined in both the legacy and canonical location."""
    counts: dict[str, int] = {}
    for path in _service_schema_modules():
        module_name = ".".join(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module_name
                and issubclass(value, BaseModel)
            ):
                key = f"{path.stem}:{name}"
                counts[key] = counts.get(key, 0) + 1

    duplicated = sorted(key for key, count in counts.items() if count > 1)
    assert not duplicated, f"service schema models defined twice: {duplicated}"
