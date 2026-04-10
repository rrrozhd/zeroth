"""Error classes for manifest validation and adapter problems.

These errors are raised when something goes wrong with a manifest (for example,
it has missing or invalid fields) or when an adapter cannot handle a manifest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.core.execution_units.validator import ManifestValidationReport


class ManifestValidationError(ValueError):
    """Raised when a manifest has errors that prevent it from being used.

    Carries the full validation report so you can inspect exactly what went
    wrong (e.g., missing fields, invalid values).
    """

    def __init__(self, report: ManifestValidationReport):
        self.report = report
        summary = report.summary()
        super().__init__(
            f"executable unit {report.unit_id!r} failed validation: "
            f"{summary['errors']} error(s), {summary['warnings']} warning(s)"
        )


class UnsupportedRuntimeAdapterError(ValueError):
    """Raised when an adapter does not know how to handle a given manifest.

    For example, trying to use the Python adapter with a command manifest.
    """
