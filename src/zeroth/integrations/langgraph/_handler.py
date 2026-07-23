"""The Zeroth governance callback handler (ZER-2, observed mode)."""

from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler


class ZerothGovernanceCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler merged into a governed graph's runtime config.

    The ZER-2 scope is deliberately narrow: this handler exists to be merged
    into a run's callback set exactly once and to receive the standard LangChain
    callback events like any other handler. It builds **no** causal ancestry
    (the ``run_id`` / ``parent_run_id`` tree is ZER-3), mints **no** attestation,
    and reports **no** governance level.

    It holds no mutable per-run state, so a single instance is safe to share
    across concurrent runs. The inherited ``on_*`` hooks from
    :class:`~langchain_core.callbacks.BaseCallbackHandler` are intentionally left
    as no-ops; the merge machinery in :mod:`._callbacks` guarantees the handler is
    registered without replacing or duplicating any user callbacks.
    """
