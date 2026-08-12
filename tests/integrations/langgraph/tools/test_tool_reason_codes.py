"""Proof that a tool-governance denial is actually recorded, not just raised.

The failure this pins is silent by construction. ``bare_error_audit_record``
turns a caught exception into ``{"reason_code": normalize_reason_code(...)}``,
and the capture boundary retains that value **only** when it is a member of
:data:`REASON_CODES`; anything else is replaced by a digest/schema/count
summary. So an unregistered exception still writes a record, still reports a
failure, and loses the one field that says *why* -- which is exactly what
shipped in ZER-5.

Nothing in the repository walks a package for exception classes, so these tests
are the enforcement. :func:`test_every_tool_governance_error_registers_an_audit_reason_code`
iterates :data:`zeroth.integrations.langgraph._tool_errors.__all__` rather than a
transcribed list, so an exception a later subtask adds to that module fails here
until its code is registered too.

:func:`test_an_unregistered_failure_is_summarized_instead_of_retained` is the
negative control: without it the round-trip assertions would also pass in a
world where the projection retained everything, and would prove nothing about
the registration.

These tests need neither ``langgraph`` nor ``langchain``, so they carry no
``langgraph_conformance`` marker -- ``addopts`` deselects that marker, and a
marked test would never run.
"""

from __future__ import annotations

import inspect

import pytest

from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.capture_vocabulary import (
    _DENIAL_REASON_CODES,
    _ZEROTH_FAILURE_CODES,
    REASON_CODES,
    normalize_reason_code,
)
from zeroth.integrations.langgraph import _tool_errors
from zeroth.runtime.orchestration.audit_recorder import bare_error_audit_record

SECRET = "sk-proj-TOOL-DENIAL-PROBE-4c1e9a"
"""Stuffed into every exception message: the message is content, never a code."""


def _exported_errors() -> tuple[type[BaseException], ...]:
    """Collect the exception classes ``_tool_errors`` exports, in declaration order."""
    exported = []
    for name in _tool_errors.__all__:
        value = getattr(_tool_errors, name)
        if inspect.isclass(value) and issubclass(value, BaseException):
            exported.append(value)
    return tuple(exported)


TOOL_ERRORS = _exported_errors()


class UnregisteredProbeError(Exception):
    """A failure deliberately absent from the vocabulary -- the negative control."""


def _record(error: BaseException, *, audit_id: str) -> NodeAuditRecord:
    """Build the record the runtime writes when it catches *error* with no audit."""
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-tool-denial",
        node_id="tool-call",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        status="failed",
        execution_metadata=bare_error_audit_record(error),
    )


async def _persisted_reason_code(database, error: BaseException, *, audit_id: str) -> object:
    """Write a bare-error record and read back the ``reason_code`` that survived."""
    repository = AuditRepository(database)
    await repository.write(_record(error, audit_id=audit_id))
    stored = await repository.list_by_run("run-tool-denial")
    match = [record for record in stored if record.audit_id == audit_id]
    assert len(match) == 1
    return match[0].execution_metadata["reason_code"]


def test_the_tool_errors_module_exports_the_five_zer6_classes() -> None:
    # Guards the walker below: if __all__ were emptied, every parametrized test
    # would vanish rather than fail.
    assert {error.__name__ for error in TOOL_ERRORS} == {
        "ApprovalRequiresThreadError",
        "GovernanceContextError",
        "PolicyViolation",
        "ToolGovernanceError",
        "UnstableToolIdentityError",
    }


@pytest.mark.parametrize("error_type", TOOL_ERRORS, ids=lambda cls: cls.__name__)
def test_every_tool_governance_error_registers_an_audit_reason_code(
    error_type: type[BaseException],
) -> None:
    # The registration check, driven off the module's own exports so a class
    # added by a later subtask is covered without editing this test.
    code = normalize_reason_code(error_type.__name__)

    assert code is not None
    assert code in REASON_CODES, f"{error_type.__name__} -> {code!r} is not registered"


def test_the_denial_verdict_and_the_failures_are_registered_in_different_sets() -> None:
    # The split is meaning, not bookkeeping: PolicyViolation is a verdict a
    # governance stage returned, the others are things that went wrong.
    assert "policy_violation" in _DENIAL_REASON_CODES
    assert "policy_violation" not in _ZEROTH_FAILURE_CODES
    assert {
        "approval_requires_thread_error",
        "governance_context_error",
        "tool_governance_error",
        "unstable_tool_identity_error",
    } <= _ZEROTH_FAILURE_CODES


@pytest.mark.parametrize("error_type", TOOL_ERRORS, ids=lambda cls: cls.__name__)
def test_every_tool_governance_error_declares_a_zeroth_namespaced_code(
    error_type: type[BaseException],
) -> None:
    # The caller-facing code, which is a different string from the audit one --
    # and specifically *how* they differ. Asserting only inequality would pass
    # on the "zeroth." prefix alone, so it would hold for any two unrelated
    # strings and could not catch a code naming the wrong condition.
    code = error_type.code  # type: ignore[attr-defined]

    assert type(code) is str
    assert code.startswith("zeroth.")
    condition = code.removeprefix("zeroth.")
    if error_type is _tool_errors.PolicyViolation:
        # The one verdict: no ``Error`` suffix on the class, deliberately, so
        # the two names are the same condition spelled once. Branching on the
        # class rather than on ``name.endswith("Error")`` -- the suffix is the
        # thing under test.
        assert condition == normalize_reason_code(error_type.__name__)
    else:
        assert f"{condition}_error" == normalize_reason_code(error_type.__name__)


@pytest.mark.parametrize("error_type", TOOL_ERRORS, ids=lambda cls: cls.__name__)
async def test_a_registered_denial_survives_the_write_into_storage(
    sqlite_db, error_type: type[BaseException]
) -> None:
    # The round trip: through bare_error_audit_record, through the capture
    # boundary AuditRepository.write applies, and back out of the database.
    expected = normalize_reason_code(error_type.__name__)

    persisted = await _persisted_reason_code(
        sqlite_db, error_type(SECRET), audit_id=f"audit:{error_type.__name__}"
    )

    assert persisted == expected


async def test_an_unregistered_failure_is_summarized_instead_of_retained(sqlite_db) -> None:
    # Without this the assertions above would pass even if the projection kept
    # every reason code, and would prove nothing about the registration.
    persisted = await _persisted_reason_code(
        sqlite_db, UnregisteredProbeError(SECRET), audit_id="audit:unregistered"
    )

    assert normalize_reason_code("UnregisteredProbeError") not in REASON_CODES
    assert set(persisted) == {"hmac_sha256", "schema", "count"}  # type: ignore[call-overload]


async def test_content_smuggled_alongside_the_reason_code_never_reaches_storage(
    sqlite_db,
) -> None:
    # Two drops, and only the second is a real test. bare_error_audit_record
    # reads type(error).__name__ and nothing else, so asserting on its output
    # alone proves only that the secret was never put there. The probe below
    # puts it there: an unallowlisted "prompt" key riding next to the reason
    # code, which NodeAuditRecord accepts at construction and the capture
    # boundary has to strip on the way into storage.
    repository = AuditRepository(sqlite_db)
    record = _record(_tool_errors.PolicyViolation(SECRET), audit_id="audit:no-message")
    smuggled = record.model_copy(
        update={"execution_metadata": {**record.execution_metadata, "prompt": SECRET}}
    )
    assert smuggled.execution_metadata["prompt"] == SECRET

    await repository.write(smuggled)
    stored = await repository.list_by_run("run-tool-denial")

    assert SECRET not in "".join(record.model_dump_json() for record in stored)
    match = [record for record in stored if record.audit_id == "audit:no-message"]
    assert match[0].execution_metadata["reason_code"] == "policy_violation"
