"""Register a deployment's agent capabilities in the bundled economics plane."""

from __future__ import annotations

from zeroth.contracts.graph.models import AgentNode, Graph
from zeroth.econ.analytics.identity import capability_identity, implementation_identity


def register_probe_economics(
    db: object,
    *,
    tenant_id: str,
    deployment_ref: str,
    capability_name: str,
    implementation_name: str,
) -> tuple[str, str]:
    """Register one tenant-qualified probe identity and return its registry keys.

    Probe callers use human-readable control identifiers.  The economics registry
    has globally keyed capability and implementation tables, so persisting those
    raw identifiers would let tenants overwrite each other's ownership.  Reuse the
    same stable tenant/deployment qualification as workflow agent nodes.
    """
    from zeroth.econ.plane.capabilities.schemas import CapabilityCreate, ImplementationCreate
    from zeroth.econ.plane.capabilities.service import create_capability, create_implementation
    from zeroth.econ.plane.scoped_session import ScopedSession

    if type(db) is not ScopedSession:
        raise TypeError("probe economics registration requires an exact ScopedSession")
    registered_capability = capability_identity(
        tenant_id,
        deployment_ref,
        capability_name,
    )
    registered_implementation = implementation_identity(
        registered_capability,
        implementation_name,
    )
    provider_name = implementation_name.partition("/")[0].lower()
    provider = provider_name if provider_name in {"openai", "anthropic"} else "custom"
    create_capability(
        db,
        CapabilityCreate(
            id=registered_capability,
            tenant_id=tenant_id,
            name=capability_name,
            category="PRODUCTIVITY",
            criticality="MED",
            description=f"Zeroth control probe in deployment {deployment_ref}",
        ),
    )
    create_implementation(
        db,
        registered_capability,
        ImplementationCreate(
            id=registered_implementation,
            tenant_id=tenant_id,
            name=implementation_name,
            provider=provider,
            model_name=implementation_name,
            model_version=implementation_name,
            config_json={
                "probe_capability": capability_name,
                "deployment_ref": deployment_ref,
            },
        ),
    )
    return registered_capability, registered_implementation


def register_graph_economics(
    graph: Graph,
    *,
    deployment_ref: str,
    tenant_id: str,
) -> None:
    """Idempotently register every instrumented agent and model pair."""
    from zeroth.econ.plane.capabilities.schemas import CapabilityCreate, ImplementationCreate
    from zeroth.econ.plane.capabilities.service import create_capability, create_implementation
    from zeroth.econ.plane.database import SessionLocal
    from zeroth.econ.plane.scoped_session import ScopedSession
    from zeroth.platform.storage.scoping import TenantWideScopeContext

    scope = (
        TenantWideScopeContext.for_default_compatibility()
        if tenant_id == "default"
        else TenantWideScopeContext(tenant_id=tenant_id)
    )
    with SessionLocal() as session:
        db = ScopedSession(session, scope)
        for node in graph.nodes:
            if not isinstance(node, AgentNode):
                continue
            capability_id = capability_identity(tenant_id, deployment_ref, node.node_id)
            model = node.agent.model_provider
            provider_name = model.partition("/")[0].lower()
            provider = provider_name if provider_name in {"openai", "anthropic"} else "custom"
            criticality = {"low": "LOW", "medium": "MED", "high": "HIGH"}[
                node.agent.criticality
            ]
            create_capability(
                db,
                CapabilityCreate(
                    id=capability_id,
                    tenant_id=tenant_id,
                    name=node.display.title or node.node_id,
                    category="PRODUCTIVITY",
                    criticality=criticality,
                    description=(
                        f"Zeroth agent node {node.node_id} in deployment {deployment_ref}"
                    ),
                ),
            )
            create_implementation(
                db,
                capability_id,
                ImplementationCreate(
                    id=implementation_identity(capability_id, model),
                    tenant_id=tenant_id,
                    name=model,
                    provider=provider,
                    model_name=model,
                    model_version=model,
                    config_json={"node_id": node.node_id, "deployment_ref": deployment_ref},
                ),
            )
