"""Declarative, fail-closed authorization policy for service routes."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, Request, status
from starlette.routing import Match

from zeroth.service.api.authorization import Permission, require_permission


def _routes(permission: Permission, *names: str) -> dict[str, Permission]:
    return dict.fromkeys(names, permission)


# Route names are stable API identity: the ordered route-inventory contract pins
# them together with path and method.  Compatibility aliases intentionally reuse
# the same endpoint name and therefore the same permission.
ROUTE_PERMISSIONS: dict[str, Permission] = {
    **_routes(
        Permission.APPROVAL_READ,
        "get_approval",
        "list_approvals",
    ),
    **_routes(Permission.APPROVAL_RESOLVE, "resolve_approval"),
    **_routes(Permission.OPERATION_RESOLVE, "resolve_ambiguous_operation"),
    **_routes(
        Permission.AUDIT_READ,
        "get_deployment_evidence",
        "get_deployment_timeline",
        "get_run_evidence",
        "get_run_timeline",
        "audit_readiness",
        "list_audits",
        "list_tenant_audits",
        "post_verify_run_chain",
        "verify_deployment_audit_chain",
        "verify_run_audit_chain",
    ),
    **_routes(
        Permission.CONNECTOR_ADMIN,
        "create_connector",
        "delete_connector",
        "seed_control_corpus",
        "test_connector",
        "update_connector",
    ),
    **_routes(
        Permission.DEPLOYMENT_ADMIN,
        "create_certification",
        "create_deployment",
        "promote_certification",
        "revoke_certification",
        "put_deployment_guardrails",
        "rollback_deployment",
    ),
    **_routes(
        Permission.DEPLOYMENT_READ,
        "get_attestation",
        "get_attestation_verify",
        "get_certification",
        "get_deployment_metadata",
        "get_deployment_guardrails",
        "get_input_contract",
        "get_identity",
        "get_manifest",
        "get_output_contract",
        "get_result_error_state_schema",
        "list_connectors",
        "list_certifications",
        "list_deployments",
        "list_manifests",
        "openapi",
        "post_verify_attestation",
        "redoc_html",
        "swagger_ui_html",
        "swagger_ui_redirect",
    ),
    **_routes(Permission.AUDIT_READ, "list_manifest_runs"),
    **_routes(Permission.CERTIFICATION_OVERRIDE, "override_certification"),
    **_routes(
        Permission.GUARDRAIL_TENANT_ADMIN,
        "get_guardrail_history",
        "get_tenant_guardrails",
        "put_tenant_guardrails",
    ),
    **_routes(
        Permission.ECON_ADMIN,
        "regulus_enforcement_approve",
        "regulus_enforcement_reject",
    ),
    **_routes(Permission.EVALUATION_ADMIN, "arm_fault"),
    **_routes(
        Permission.ENFORCEMENT_REPORT,
        "attest_run_start",
        "decide_tool_call",
        "read_deployment_status",
        "read_run_status",
        "register_tool_inventory",
        "report_heartbeat",
    ),
    **_routes(
        Permission.METRICS_ADMIN,
        "attach_quality_verdict",
        "set_tenant_budget",
    ),
    **_routes(
        Permission.METRICS_READ,
        "econ_dashboard_action_suppression",
        "econ_dashboard_calibration_trend",
        "econ_dashboard_capability_ranking",
        "econ_dashboard_capital_destroyers",
        "econ_dashboard_confidence_trend",
        "econ_dashboard_data_quality_mix",
        "econ_dashboard_efficiency_trend",
        "econ_dashboard_kpis",
        "econ_dashboard_top_creators",
        "get_latest_rightsizing_experiment",
        "regulus_costing_estimate",
        "regulus_costing_profile",
        "regulus_dashboard_action_suppression",
        "regulus_dashboard_calibration_trend",
        "regulus_dashboard_capability_ranking",
        "regulus_dashboard_capital_destroyers",
        "regulus_dashboard_confidence_gate",
        "regulus_dashboard_confidence_trend",
        "regulus_dashboard_data_quality",
        "regulus_dashboard_drift_timeline",
        "regulus_dashboard_efficiency_trend",
        "regulus_dashboard_implementation_compare",
        "regulus_dashboard_kpis",
        "regulus_dashboard_policy_timeline",
        "regulus_dashboard_top_creators",
        "regulus_enforcement_actions",
        "regulus_enforcement_policy_actions",
        "regulus_evaluations_history",
        "regulus_evaluations_latest",
        "regulus_performance_capabilities",
        "regulus_performance_summary",
        "regulus_reconciliation_calibration",
        "regulus_registry_capabilities",
        "regulus_registry_capability",
        "regulus_registry_implementation",
        "get_deployment_cost",
        "get_economics_configuration",
        "get_metrics",
        "get_tenant_cost",
        "get_unit_economics",
        "get_waste",
        "rightsizing_opportunities",
    ),
    **_routes(
        Permission.RETENTION_ADMIN,
        "get_retention_policy",
        "list_erasure_history",
        "list_legal_holds",
        "place_legal_hold",
        "put_retention_policy",
        "release_legal_hold",
        "request_erasure",
    ),
    **_routes(
        Permission.RUN_ADMIN,
        "cancel_run",
        "interrupt_run",
        "replay_run",
    ),
    **_routes(
        Permission.RUN_CREATE,
        "attest",
        "create_run",
        "decide",
        "heartbeat",
        "langgraph-gateway",
        "langgraph-protocol-events",
        "register_inventory",
    ),
    **_routes(
        Permission.RUN_READ,
        "get_artifact",
        "get_run",
        "get_template",
        "list_child_runs",
        "list_admin_runs",
        "list_templates",
    ),
    **_routes(
        Permission.TEMPLATE_ADMIN,
        "create_template",
        "delete_template",
    ),
    **_routes(
        Permission.WEBHOOK_ADMIN,
        "create_subscription",
        "deactivate_subscription",
        "get_subscription",
        "list_dead_letters",
        "list_deliveries",
        "list_subscriptions",
        "replay_dead_letter",
    ),
    **_routes(
        Permission.WORKFLOW_ADMIN,
        "clone_workflow",
        "create_contract",
        "create_workflow",
        "delete_workflow",
        "publish_workflow",
        "update_workflow",
        "verify_workflow_providers",
    ),
    **_routes(
        Permission.WORKFLOW_READ,
        "diff_workflow",
        "get_workflow",
        "list_contracts",
        "list_node_types",
        "list_workflows",
        "preflight_workflow",
        "suggest_rightsizing",
    ),
    **_routes(Permission.METRICS_ADMIN, "run_rightsizing_experiment"),
}

PUBLIC_ROUTE_NAMES = frozenset({"health", "health_live", "health_ready"})
PUBLIC_ROUTE_PATHS = frozenset({"/console"})

# The mounted econ application owns a separate JWT authorization boundary.  The
# outer Zeroth middleware still authenticates callers; this exemption avoids
# pretending the unresolved econ global-vs-tenant policy belongs to this table.
DELEGATED_MOUNT_PATHS = frozenset({"/regulus"})


def permission_for_route_name(name: str | None) -> Permission | None:
    """Return a declared permission, or ``None`` only for an explicit public route."""
    if name in PUBLIC_ROUTE_NAMES:
        return None
    if not isinstance(name, str) or name not in ROUTE_PERMISSIONS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return ROUTE_PERMISSIONS[name]


def route_authorization_disposition(route: object) -> tuple[Permission | None, str]:
    """Return the authoritative permission and disposition for one route."""
    path = getattr(route, "path", None)
    if path in DELEGATED_MOUNT_PATHS:
        return None, "delegated"
    if path in PUBLIC_ROUTE_PATHS:
        return None, "public"
    permission = permission_for_route_name(getattr(route, "name", None))
    return permission, "public" if permission is None else "permission"


def matching_route(routes: Iterable[object], scope: dict[str, object]) -> object | None:
    """Return the first full Starlette route match, preserving registration order."""
    for route in routes:
        match, _ = route.matches(scope)  # type: ignore[attr-defined]
        if match is Match.FULL:
            return route
    return None


async def authorize_matched_route(request: Request) -> None:
    """Enforce coarse route RBAC after authentication and before dispatch."""
    route = matching_route(request.app.routes, request.scope)
    if route is None:
        return
    permission, disposition = route_authorization_disposition(route)
    if disposition in {"delegated", "public"}:
        return
    assert permission is not None
    # Endpoint-level checks still own deployment/resource scope.  This router
    # layer closes missing-RBAC gaps without changing their hiding contracts.
    await require_permission(request, permission, enforce_deployment_scope=False)


__all__ = [
    "DELEGATED_MOUNT_PATHS",
    "PUBLIC_ROUTE_NAMES",
    "PUBLIC_ROUTE_PATHS",
    "ROUTE_PERMISSIONS",
    "authorize_matched_route",
    "matching_route",
    "permission_for_route_name",
    "route_authorization_disposition",
]
