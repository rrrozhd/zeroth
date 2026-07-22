"""Legacy import location for the template api module.

The definitions now live in :mod:`zeroth.service.api.template_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.template_api import CreateTemplateRequest as CreateTemplateRequest
from zeroth.service.api.template_api import TemplateListResponse as TemplateListResponse
from zeroth.service.api.template_api import TemplateResponse as TemplateResponse
from zeroth.service.api.template_api import _template_registry as _template_registry
from zeroth.service.api.template_api import register_template_routes as register_template_routes
