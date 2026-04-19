"""Unified configuration for the Zeroth platform.

Loads settings from YAML defaults, .env file overrides, and environment
variable overrides (highest priority) using pydantic-settings.
"""

from zeroth.core.config.settings import ZerothSettings, get_settings

__all__ = ["ZerothSettings", "get_settings"]
