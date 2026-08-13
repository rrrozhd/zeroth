"""Refusal types shared by the econ revisions and the runtime convergence path.

These live here rather than in the revision files that raise them because the
runtime loads a revision **by path, under a synthetic module name, once per
call** (``database._load_compat_migration``, which deliberately does not cache:
``migration.op`` is module-global and sync FastAPI dependencies run in a
threadpool, so a shared module would let one thread's DDL run against another's
connection).

Every such load builds a *new* class object.  A refusal defined inside the
revision is therefore only catchable against the very same load -- and
``except <that load>.SomeError`` silently stops matching the moment the raise
and the catch come from two loads.  A convergence refusal that escapes takes the
whole plane down at startup, so the failure mode is the opposite of the
containment it was written for.

Defining them once in an ordinary importable module makes the identity stable no
matter how many times a revision is loaded: an absolute import resolves to the
same class object regardless of how the importing module itself was loaded.
"""

from __future__ import annotations


class DuplicatePolicyActionLink(RuntimeError):
    """Raised when ``policy_actions`` already breaks the one-per-action invariant.

    A dedicated type so a caller that contains this condition contains *only*
    this condition: a bare ``RuntimeError`` from ``op.create_index`` (a bad
    connection, a lock timeout) would otherwise be reported to the operator as
    "rows collide", which is a different problem with a different remedy.
    """


class DuplicateOutcomeIdentity(RuntimeError):
    """Raised when ``outcome_events`` already violates the identity being built.

    Same rationale as :class:`DuplicatePolicyActionLink`: narrow enough that
    containing it cannot swallow an unrelated failure of the same base type.
    """
