"""Studio authoring bootstrap wiring."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from zeroth.contracts import ContractRegistry
from zeroth.graph import GraphRepository
from zeroth.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig, ServiceAuthenticator
from zeroth.storage import SQLiteDatabase
from zeroth.studio.app import create_studio_app
from zeroth.studio.leases.service import WorkflowLeaseService
from zeroth.studio.workflows.repository import WorkflowRepository
from zeroth.studio.workflows.service import WorkflowService


@dataclass(slots=True)
class StudioBootstrap:
    """Container for the Studio authoring HTTP surface."""

    workflow_service: WorkflowService
    lease_service: WorkflowLeaseService
    graph_repository: GraphRepository
    contract_registry: ContractRegistry
    auth_config: ServiceAuthConfig
    authenticator: ServiceAuthenticator


def bootstrap_studio(
    database: SQLiteDatabase,
    *,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> StudioBootstrap:
    """Build Studio authoring services and auth wiring."""
    graph_repository = GraphRepository(database)
    workflow_repository = WorkflowRepository(database)
    workflow_service = WorkflowService(
        workflow_repository=workflow_repository,
        graph_repository=graph_repository,
    )
    lease_service = WorkflowLeaseService(workflow_repository=workflow_repository)
    contract_registry = ContractRegistry(database)
    resolved_auth_config = auth_config or ServiceAuthConfig.from_env()
    authenticator = ServiceAuthenticator(
        resolved_auth_config,
        bearer_verifier=bearer_token_verifier,
    )
    return StudioBootstrap(
        workflow_service=workflow_service,
        lease_service=lease_service,
        graph_repository=graph_repository,
        contract_registry=contract_registry,
        auth_config=resolved_auth_config,
        authenticator=authenticator,
    )


def bootstrap_studio_app(
    database: SQLiteDatabase,
    *,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> FastAPI:
    """Build the FastAPI app for the Studio authoring surface."""
    return create_studio_app(
        bootstrap_studio(
            database,
            auth_config=auth_config,
            bearer_token_verifier=bearer_token_verifier,
        )
    )
