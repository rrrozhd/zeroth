"""Legacy import location for the service authorization surface.

The definitions now live in :mod:`zeroth.service.api.authorization`. This
module republishes the same objects, so the protected legacy import path
keeps resolving to identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.authorization import (
    BUILTIN_ROLE_PERMISSIONS as BUILTIN_ROLE_PERMISSIONS,
)
from zeroth.service.api.authorization import (
    DEFAULT_ROLE_REGISTRY as DEFAULT_ROLE_REGISTRY,
)
from zeroth.service.api.authorization import (
    ROLE_PERMISSIONS as ROLE_PERMISSIONS,
)
from zeroth.service.api.authorization import (
    Permission as Permission,
)
from zeroth.service.api.authorization import RoleRegistry as RoleRegistry
from zeroth.service.api.authorization import (
    require_deployment_scope as require_deployment_scope,
)
from zeroth.service.api.authorization import (
    require_permission as require_permission,
)
from zeroth.service.api.authorization import (
    require_resource_scope as require_resource_scope,
)
