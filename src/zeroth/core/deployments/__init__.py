"""Legacy import path for the deployment service domain.

Deployments live in :mod:`zeroth.service.deployments`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.service.deployments import (
    Deployment,
    DeploymentEngineMode,
    DeploymentError,
    DeploymentService,
    DeploymentStatus,
    SQLiteDeploymentRepository,
)

__all__ = [
    "Deployment",
    "DeploymentEngineMode",
    "DeploymentError",
    "DeploymentService",
    "DeploymentStatus",
    "SQLiteDeploymentRepository",
]
