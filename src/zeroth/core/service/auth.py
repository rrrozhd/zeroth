"""Legacy import location for service authentication.

The definitions now live in :mod:`zeroth.service.api.authentication`. This
module republishes the same objects, so the protected legacy import path
keeps resolving to identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.authentication import (
    AuthenticationError as AuthenticationError,
)
from zeroth.service.api.authentication import (
    BearerTokenConfig as BearerTokenConfig,
)
from zeroth.service.api.authentication import (
    JWTBearerTokenVerifier as JWTBearerTokenVerifier,
)
from zeroth.service.api.authentication import (
    ServiceAuthConfig as ServiceAuthConfig,
)
from zeroth.service.api.authentication import (
    ServiceAuthenticator as ServiceAuthenticator,
)
from zeroth.service.api.authentication import (
    StaticApiKeyCredential as StaticApiKeyCredential,
)
from zeroth.service.api.authentication import (
    current_principal as current_principal,
)
from zeroth.service.api.authentication import (
    record_service_denial as record_service_denial,
)
