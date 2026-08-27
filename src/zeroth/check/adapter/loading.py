"""Controlled import and validation of ``module:build_target`` entrypoints."""

from __future__ import annotations

import importlib
import inspect

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph import LangGraphCheckTarget
from zeroth.check.tape.normalization import sha256_digest


class TargetLoadError(RuntimeError):
    """A configured target violates the V1 build contract."""


def load_target(entrypoint: str, bindings: CheckBindings) -> LangGraphCheckTarget:
    if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
        raise TargetLoadError("target must use module:build_target syntax")
    module_name, attribute = entrypoint.split(":", 1)
    if attribute != "build_target" or not module_name:
        raise TargetLoadError("target entrypoint must be named build_target")
    try:
        module = importlib.import_module(module_name)
        builder = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise TargetLoadError(f"cannot import target {entrypoint}") from exc
    if not callable(builder):
        raise TargetLoadError("build_target must be callable")
    parameters = list(inspect.signature(builder).parameters.values())
    if len(parameters) != 1 or parameters[0].name != "bindings":
        raise TargetLoadError("build_target must accept exactly one parameter named bindings")
    try:
        target = builder(bindings)
    except Exception as exc:
        raise TargetLoadError("build_target failed") from exc
    if type(target) is not LangGraphCheckTarget:
        raise TargetLoadError("build_target must return LangGraphCheckTarget")
    bindings.freeze()
    try:
        source = inspect.getsource(builder)
    except (OSError, TypeError) as exc:
        raise TargetLoadError("build_target source is unavailable for fingerprinting") from exc
    target.entrypoint_digest = sha256_digest({"entrypoint": entrypoint, "source": source})
    return target
