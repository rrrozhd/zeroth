"""Legacy import location for the service application factory.

The composition now lives in :mod:`zeroth.service.app`, with the lifespan in
:mod:`zeroth.service.bootstrap.lifecycle` and the wrapper health schema in
:mod:`zeroth.service.api.health`. This module republishes the same objects, so
the protected legacy import path keeps resolving to identical objects.
"""

from __future__ import annotations

from zeroth.service.api.health import HealthResponse as HealthResponse
from zeroth.service.app import ServiceBootstrapLike as ServiceBootstrapLike
from zeroth.service.app import create_app as create_app
from zeroth.service.app import service_lifespan as service_lifespan
