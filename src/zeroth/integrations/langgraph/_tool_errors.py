"""Typed failures raised by governed LangGraph tool enforcement.

**Each class here carries two different strings, and confusing them is the bug
this module exists to make hard to write.**

* :attr:`ToolGovernanceError.code` -- the ``zeroth.``-prefixed *caller-facing*
  code, the namespace the gateway's error envelope requires
  (:class:`~zeroth.contracts.langgraph_gateway.models.GatewayError` rejects a code
  that does not start with ``zeroth.``). It names the condition, without the
  ``Error`` suffix, matching ``zeroth.policy_denied`` and the rest.
* The *audit* reason code, which nothing here spells out: it is derived from the
  class name by
  :func:`~zeroth.governance.audit.capture_vocabulary.normalize_reason_code`,
  because that is what
  :func:`~zeroth.runtime.orchestration.audit_recorder.bare_error_audit_record`
  does to any exception it catches. ``ApprovalRequiresThreadError`` becomes
  ``approval_requires_thread_error`` there and ``zeroth.approval_requires_thread``
  here.

**The audit code has to be registered or the denial is not recorded.** A
normalized code absent from
:data:`~zeroth.governance.audit.capture_vocabulary.REASON_CODES` is replaced by
a digest/schema/count summary at the capture boundary -- the record still
writes, the failure still looks audited, and the reason is gone. Every class
below is registered in ``capture_vocabulary``: the four ``*Error`` names among
the package's failure codes, and :class:`PolicyViolation` among the denial
reason codes, because a policy denial is a verdict a governance stage returned
rather than something that went wrong.

Adding an exception to this module means adding its normalized name there in the
same commit. ``tests/integrations/langgraph/tools/test_tool_reason_codes.py``
walks :data:`__all__` and fails until that is done.
"""

from __future__ import annotations

from typing import ClassVar


class ToolGovernanceError(Exception):
    """Base for every failure the governed tool path raises.

    Callers catch this to mean "the tool call did not proceed, and governance is
    why". It is concrete rather than abstract so an enforcement stage with no
    more specific diagnosis still raises something registered.
    """

    code: ClassVar[str] = "zeroth.tool_governance"


class PolicyViolation(ToolGovernanceError):  # noqa: N818 - a verdict, not a malfunction.
    """A tool call was refused because policy denied it.

    The one *verdict* in this module: nothing malfunctioned, a governance stage
    decided. That is why its audit reason code is registered among the denial
    reasons rather than among the package's failures, and why the class is not
    named ``...Error``.
    """

    code: ClassVar[str] = "zeroth.policy_violation"


class GovernanceContextError(ToolGovernanceError):
    """The governance context needed to attribute a tool call was missing or unusable.

    Raised instead of falling back to an unattributed allow: a call whose
    principal, tenant or run cannot be established is not a call that can be
    decided.
    """

    code: ClassVar[str] = "zeroth.governance_context"


class ApprovalRequiresThreadError(ToolGovernanceError):
    """A tool call needs human approval, but the run has no thread to resume into.

    :attr:`~zeroth.integrations.langgraph._tool_types.ToolDecisionKind.REQUIRE_APPROVAL`
    is only answerable when execution can be interrupted and later resumed. A
    threadless run cannot do that, so the call fails rather than proceeding
    unapproved.
    """

    code: ClassVar[str] = "zeroth.approval_requires_thread"


class UnstableToolIdentityError(ToolGovernanceError):
    """A tool's identity could not be pinned to something a policy can be written against.

    A tool whose fingerprint changes between observations cannot carry a
    reproducible decision, so it is refused rather than decided against an
    identity that will not hold.
    """

    code: ClassVar[str] = "zeroth.unstable_tool_identity"


class DuplicateToolExecutionError(ToolGovernanceError):
    """A stable side-effecting call is already executing or has an unknown outcome."""

    code: ClassVar[str] = "zeroth.duplicate_tool_execution"


__all__ = [
    "ApprovalRequiresThreadError",
    "DuplicateToolExecutionError",
    "GovernanceContextError",
    "PolicyViolation",
    "ToolGovernanceError",
    "UnstableToolIdentityError",
]
