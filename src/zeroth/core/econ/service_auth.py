"""Legacy import path for :mod:`zeroth.econ.analytics.service_auth`."""

from zeroth.econ.analytics.service_auth import (
    HeadersProvider,
    make_self_auth_headers_provider,
    mint_econ_service_token,
)

__all__ = [
    "HeadersProvider",
    "make_self_auth_headers_provider",
    "mint_econ_service_token",
]
