"""Unified configuration for the Zeroth platform.

Loads settings from YAML defaults, .env file overrides, and environment
variable overrides (highest priority) using pydantic-settings.
"""

from zeroth.platform.config.settings import LangGraphGatewaySettings, ZerothSettings, get_settings

__all__ = ["LangGraphGatewaySettings", "ZerothSettings", "get_settings"]
