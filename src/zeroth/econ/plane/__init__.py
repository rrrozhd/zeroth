"""Economic control-plane service.

The bundled Regulus control plane (see PROVENANCE.md) merged with the
econ-event erasure adapter that already lived in this package. The FastAPI
backend is :mod:`zeroth.econ.plane.main`; the service layer mounts it under
``/regulus`` and its settings keep the ``ECP_`` environment prefix.
"""

__all__ = ["main"]
