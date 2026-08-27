"""Provider-neutral trajectory checking primitives.

Framework adapters are intentionally not imported here so the core package remains usable
without LangGraph installed.
"""

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.config import CheckConfig, load_check_config

__all__ = ["CheckBindings", "CheckConfig", "load_check_config"]
