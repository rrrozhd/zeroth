"""Legacy import path for the economic control plane.

The control plane lives in :mod:`zeroth.econ.plane`; this package
republishes the same modules for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""


def __getattr__(name: str) -> object:
    if name == "main":
        import importlib

        return importlib.import_module("zeroth.econ.plane.main")
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
