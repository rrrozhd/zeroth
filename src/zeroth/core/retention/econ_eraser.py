"""Legacy import path for :mod:`zeroth.governance.retention.econ_eraser`.

``SqlAlchemyEconEventEraser`` resolves lazily through the canonical module,
which itself defers to :mod:`zeroth.econ.plane.erasure`, so this shim keeps
no import edge into the econ domain.
"""

from zeroth.governance.retention.econ_eraser import EconEventEraser as EconEventEraser


def __getattr__(name: str) -> object:
    """Resolve the concrete econ-plane adapter through the canonical module."""
    if name == "SqlAlchemyEconEventEraser":
        import zeroth.governance.retention.econ_eraser as econ_eraser

        return econ_eraser.SqlAlchemyEconEventEraser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({"EconEventEraser", "SqlAlchemyEconEventEraser"})
