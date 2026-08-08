"""Proof that the two install surfaces decide one scenario the same way (R8).

**One table, two drivers, one equality.** :data:`SCENARIOS` is the single shared
structure: every scenario in it is driven through ``govern_tools`` *and* through
``ZerothMiddleware`` inside the same test body, and the first assertion is that
the two :class:`Outcome` values are equal. Only after that does the test check
the outcome against what the scenario declared. Two parametrized tests over the
same list would prove each surface matches a written-down expectation; one
equality proves they match *each other*, which is the property that keeps a
fail-closed rule from drifting on one surface and not the other.

**What an outcome compares.** The exception's exact class -- not "both raised" --
the returned value, how many times the downstream ran, the full audit projection
of every record emitted, and every interrupt payload. The audit projection is the
record ``model_dump`` minus exactly two fields: :data:`_VOLATILE_FIELDS` holds
``audit_id`` (a fresh ``uuid4`` per emission) and ``started_at`` (wall clock).
Nothing else is excluded, and nothing else may be: a divergence in any other
field is precisely what this test exists to catch, so the fix for a failure here
is the code, never the exclude set.

**Each surface is driven through its own doubles.** The scenario carries a
*factory* for its decision client, and each driver builds its own interrupt seam,
audit sink and resolver cell, so a consultation count, a recorded payload or a
mutated fact from one surface can never leak into the other's outcome.

**Two scenarios are about *when* a fact is resolved, not what it is.** A
governance fact that a surface reads once at install time and another reads per
call makes the two decide the same tool differently the moment the fact moves --
and the stale reading is always the permissive one. Those scenarios supply a live
resolver through :attr:`Scenario.resolvers` and change what it answers through
:attr:`Scenario.mutate`, in the window between installing the tool and calling
it. They need a decision client whose *verdict* turns on the fact, because the
audit projection of an allow carries neither the side-effect class nor the
contract ref; see :class:`SideEffectSensitiveClient` for the shape.

**Why this is tractable at all.** Both surfaces derive tool identity through
``_tool_wrappers._describe_base_tool``, so one tool fingerprints identically
whichever way it is installed
(``test_middleware.test_one_tool_fingerprints_identically_on_both_install_surfaces``).
That is what lets the same tool object be handed to both drivers below and lets
the audit projections and interrupt payloads compare byte for byte.

**Tier A.** This suite needs ``langchain.agents``, which ships only in the
optional ``gateway-conformance`` group, so it carries both guards: an
``importorskip`` before any langchain import and the ``langgraph_conformance``
marker. ``addopts`` deselects that marker, so these tests do **not** run in the
default suite (or in the pre-commit hook); run them with
``-o addopts= -m langgraph_conformance``.

**The guard names the module this file actually imports.** A marked module is
imported at *collection*, so skipping on ``langgraph`` while importing
``langchain.agents`` errors instead of skipping in an environment that has the
first and not the second. Guarding ``langchain.agents`` covers both, because
importing it pulls ``langgraph`` in and ``importorskip`` skips on either failure.
"""

from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langchain.agents", reason="requires the gateway-conformance dependency group")

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import HumanMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, InjectedStore
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, field_validator

from tests.integrations.langgraph.tools._agents import TOOL_CALL_ID, scripted_model
from zeroth.governance.audit import NodeAuditRecord
from zeroth.integrations.langgraph._approval_lifecycle import SQLiteApprovalRepository
from zeroth.integrations.langgraph._middleware import ZerothMiddleware
from zeroth.integrations.langgraph._tool_decisions import UnknownSideEffectPolicy
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._tool_wrappers import govern_tools

pytestmark = pytest.mark.langgraph_conformance

BODY_RESULT = "body-result"
"""What an allowed call returns on both surfaces, so the value is comparable too."""

THREADED = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
    correlation_id="corr-1",
)
"""A run that can be paused: approval needs a thread to resume into."""

THREADLESS = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")
APPROVE = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)

_VOLATILE_FIELDS = {
    "audit_id": True,
    "started_at": True,
    "approval_actions": {"__all__": {"occurred_at"}},
}
"""The only three record fields a projection drops, because only these three vary.

``audit_id`` is a fresh ``uuid4`` per emission, ``started_at`` is wall clock, and
``ApprovalActionRecord.occurred_at`` is a per-record wall clock nested one level
down -- which is why the exclusion is nested rather than a flat name. Every other
field is a governance fact both surfaces must agree on, so widening this set to
make a comparison pass would delete the assertion instead of fixing the
divergence it found. Each of the three is excluded because it is a *timestamp or
a nonce*, and nothing else qualifies.
"""


class Suspended(Exception):  # noqa: N818 - a pause, not a malfunction.
    """Stands in for LangGraph's ``GraphInterrupt``, which is what a real pause raises."""


@dataclasses.dataclass
class FixedClient:
    """A decision client that returns one configured verdict, however often it is asked."""

    verdict: object = ALLOW

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the configured verdict."""
        return self.verdict  # type: ignore[return-value]


@dataclasses.dataclass
class RaisingClient:
    """A decision client that cannot answer, which is a denial and never a pass."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Fail to reach the policy source."""
        raise RuntimeError("the policy source is unreachable")


@dataclasses.dataclass
class RecordingInterrupt:
    """A pause seam that records the payload and suspends by raising, as LangGraph does."""

    payloads: list[Any] = dataclasses.field(default_factory=list)

    def __call__(self, payload: Any) -> Any:
        """Record *payload* and suspend."""
        self.payloads.append(dict(payload))
        raise Suspended


@dataclasses.dataclass
class RecordingSubmitter:
    """An audit sink that keeps every record the enforcement core handed it."""

    records: list[NodeAuditRecord] = dataclasses.field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        """Keep *record*, as the delivery queue's non-blocking hand-off does."""
        self.records.append(record)


@dataclasses.dataclass
class Body:
    """The tool body the wrapper surface's downstream reaches, counting executions."""

    calls: int = 0

    def run(self, **_kwargs: Any) -> str:
        """Count this execution and return the shared result."""
        self.calls += 1
        return BODY_RESULT


@dataclasses.dataclass
class Handler:
    """``ToolNode`` as the middleware surface actually meets it: it runs ``request.tool``.

    A handler that merely returned a value would measure nothing on this surface
    any more. ``ZerothMiddleware`` no longer decides; it substitutes a governed
    twin into the request, and the *downstream* executes it -- so a double that
    ignores ``request.tool`` reports an allow for every verdict and compares
    equal to nothing the wrapper surface does.

    The raw argument mapping is handed to ``invoke`` rather than the whole tool
    call, because that is the shape ``drive_wrapper`` hands the wrapper surface;
    driving the two with different inputs would compare the inputs rather than
    the surfaces.
    """

    calls: int = 0

    def __call__(self, request: Any) -> Any:
        """Count this invocation and execute whatever tool the request now carries."""
        self.calls += 1
        return request.tool.invoke(dict(request.tool_call["args"]))

    async def acall(self, request: Any) -> Any:
        """The awaitable form of the same downstream."""
        self.calls += 1
        await asyncio.sleep(0)
        return await request.tool.ainvoke(dict(request.tool_call["args"]))


def read_only(_target: object) -> SideEffectClass:
    """Classify every tool as read-only, so the allow path needs no blanket opt-in.

    Stateless, because most scenarios are about the *verdict* and want nothing
    else moving. The two scenarios that are about resolution *timing* install
    their own resolvers instead -- see :func:`drifting_classification` and
    :func:`drifting_contract`, and :attr:`Scenario.mutate` for how the change is
    staged.

    An earlier revision of this file said resolvers here must be stateless
    because the wrapper pinned the classification at wrap time while the
    middleware re-ran it per call. That asymmetry was the bug (a tool that turned
    side-effecting after wrapping was still decided as read-only on the wrapper
    surface), and the note that excused it from the table is exactly why nothing
    caught it. Both surfaces now resolve every authorization fact per call, and
    the table asserts it.
    """
    return SideEffectClass.READ_ONLY


PERMITTED_CONTRACT = "contract:v1"
REVOKED_CONTRACT = "contract:v2"
"""One contract a policy is bound to, and the one it is not."""


@dataclasses.dataclass
class SideEffectSensitiveClient:
    """A policy that permits a read-only tool and refuses a side-effecting one.

    The classification has to *change the verdict* for a resolution-timing
    divergence to be observable at all: neither the audit projection nor the
    interrupt payload of an allow carries the side-effect class, so a surface
    deciding on a stale classification and allowing would otherwise compare equal
    to a surface deciding on the current one and allowing.
    """

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Refuse a side-effecting call, permit anything else."""
        return DENY if action.side_effect is SideEffectClass.SIDE_EFFECTING else ALLOW


@dataclasses.dataclass
class ContractSensitiveClient:
    """A policy bound to one contract, refusing a call that carries any other."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Permit only the contract this policy was written against."""
        return ALLOW if action.contract_ref == PERMITTED_CONTRACT else DENY


@dataclasses.dataclass
class CapabilitySensitiveClient:
    """A policy that permits only the reviewed capability set."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Permit only the capability set this policy reviewed."""
        return ALLOW if tuple(action.capability_refs) == ("capability:read",) else DENY


@dataclasses.dataclass
class ApprovalSensitiveClient:
    """A policy that pauses only actions whose metadata requires approval."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Require approval exactly when the action says it is required."""
        return APPROVE if action.requires_approval else ALLOW


def drifting_classification(live: dict[str, Any]) -> dict[str, Any]:
    """Install a classifier that reads a cell, starting the tool out read-only.

    An externally flipped cell rather than a counter that advances per call: the
    two surfaces do not consult their resolvers the same number of times (the
    wrapper also reads them once at wrap time, for the inventory binding), so a
    counter would manufacture a difference that is about call counts rather than
    about *when* the fact is resolved.

    Args:
        live: The cell this scenario's resolvers read, mutated in place.

    Returns:
        The seam overrides this scenario installs on both surfaces.
    """
    live["side_effect"] = SideEffectClass.READ_ONLY
    return {"side_effect": lambda _target: live["side_effect"]}


def reclassify(live: dict[str, Any]) -> None:
    """Turn the tool side-effecting, after it is installed and before it is called."""
    live["side_effect"] = SideEffectClass.SIDE_EFFECTING


def drifting_contract(live: dict[str, Any]) -> dict[str, Any]:
    """Install a contract resolver that reads a cell, starting on the permitted contract.

    Args:
        live: The cell this scenario's resolvers read, mutated in place.

    Returns:
        The seam overrides this scenario installs on both surfaces.
    """
    live["contract"] = PERMITTED_CONTRACT
    return {"side_effect": read_only, "contract_ref": lambda _target: live["contract"]}


def rebind_contract(live: dict[str, Any]) -> None:
    """Move the tool onto a contract the policy does not permit."""
    live["contract"] = REVOKED_CONTRACT


def drifting_capabilities(live: dict[str, Any]) -> dict[str, Any]:
    """Install a required-capability resolver backed by a mutable cell."""
    live["capabilities"] = ("capability:read",)
    return {
        "side_effect": read_only,
        "capability_refs": lambda _target: live["capabilities"],
    }


def require_more_capabilities(live: dict[str, Any]) -> None:
    """Change the required capability after installation."""
    live["capabilities"] = ("capability:write",)


def drifting_approval(live: dict[str, Any]) -> dict[str, Any]:
    """Install an approval resolver backed by a mutable cell."""
    live["approval"] = False
    return {
        "side_effect": read_only,
        "requires_approval": lambda _target: live["approval"],
    }


def require_approval(live: dict[str, Any]) -> None:
    """Require approval after installation."""
    live["approval"] = True


def build_tool(body: Body) -> StructuredTool:
    """Build the sync ``BaseTool`` both surfaces are driven against."""

    def _run(query: str) -> str:
        return body.run(query=query)

    return StructuredTool.from_function(func=_run, name="search", description="search for things.")


def build_async_tool(body: Body) -> StructuredTool:
    """Build the async ``BaseTool`` both surfaces are driven against."""

    async def _arun(query: str) -> str:
        return body.run(query=query)

    return StructuredTool.from_function(
        coroutine=_arun, name="search", description="search for things."
    )


def build_dual_tool(body: Body) -> StructuredTool:
    """Build one tool that carries both bodies, for driving *one* tool down both paths.

    A tool's identity is bound to the code it will run, so a sync-only tool and an
    async-only tool are two different tools with two different fingerprints -- and
    the fingerprint is recorded in every audit record. Comparing the sync and
    async halves of a surface therefore has to compare one tool driven twice, not
    two tools driven once each; otherwise the difference the comparison finds is
    the tools', not the surface's.
    """

    def _run(query: str) -> str:
        return body.run(query=query)

    async def _arun(query: str) -> str:
        return body.run(query=query)

    return StructuredTool.from_function(
        func=_run, coroutine=_arun, name="search", description="search for things."
    )


def build_request(tool: Any) -> ToolCallRequest:
    """Build the middleware request LangChain would hand in for one tool call."""
    return ToolCallRequest(
        tool_call={
            "name": tool.name,
            "args": {"query": "cats"},
            "id": "call-1",
            "type": "tool_call",
        },
        tool=tool,
        state={},
        runtime=None,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class Scenario:
    """One governed call, described once and driven through both install surfaces.

    Attributes:
        label: The scenario's name, used as the parametrization id.
        client: Builds the decision client. A factory rather than an instance, so
            each surface is driven through doubles of its own.
        context: The governance context, or ``None`` for a call nobody attributed.
        classify: The side-effect classifier, or ``None`` to leave the tool
            unclassified.
        unknown_side_effect: Whether a tool nobody classified may be invoked.
        error: The exception class both surfaces must raise, or ``None`` when the
            call proceeds.
        downstream_calls: How many times the downstream must run.
        records: How many audit records the decision must produce.
        decision: The ``decision`` term the emitted record carries, or ``None``
            when nothing is recorded.
        interrupts: How many approval payloads the pause seam must receive.
        resolvers: Given a fresh cell of this scenario's own, returns the seam
            overrides it installs -- the hook a scenario about *when* a fact is
            resolved uses to supply a live resolver. ``None`` leaves the seams as
            *classify* declared them.
        mutate: Called with the same cell after the surface is built and before
            it is invoked, so the change lands in the window between installing a
            tool and calling it. That window is the whole subject: a surface that
            resolves at install time decides the old fact, and the old fact is
            always the permissive one.
    """

    label: str
    client: Callable[[], Any]
    context: ToolGovernanceContext | None
    classify: Callable[[Any], SideEffectClass] | None
    unknown_side_effect: UnknownSideEffectPolicy
    error: type[Exception] | None
    downstream_calls: int
    records: int
    decision: str | None
    interrupts: int
    resolvers: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    mutate: Callable[[dict[str, Any]], None] | None = None


SCENARIOS = (
    Scenario(
        label="allow",
        client=lambda: FixedClient(ALLOW),
        context=THREADED,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=None,
        downstream_calls=1,
        records=1,
        decision="allow",
        interrupts=0,
    ),
    Scenario(
        label="deny",
        client=lambda: FixedClient(DENY),
        context=THREADED,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=PolicyViolation,
        downstream_calls=0,
        records=1,
        decision="deny",
        interrupts=0,
    ),
    Scenario(
        label="require-approval",
        client=lambda: FixedClient(APPROVE),
        context=THREADED,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=Suspended,
        downstream_calls=0,
        records=1,
        decision="require_approval",
        interrupts=1,
    ),
    Scenario(
        label="require-approval-without-a-thread",
        client=lambda: FixedClient(APPROVE),
        context=THREADLESS,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=ApprovalRequiresThreadError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
    ),
    Scenario(
        label="unknown-side-effect-denied",
        client=lambda: FixedClient(ALLOW),
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=PolicyViolation,
        downstream_calls=0,
        records=1,
        decision="deny",
        interrupts=0,
    ),
    Scenario(
        label="unknown-side-effect-admitted-by-the-named-opt-in",
        client=lambda: FixedClient(ALLOW),
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
        error=None,
        downstream_calls=1,
        records=1,
        decision="allow",
        interrupts=0,
    ),
    Scenario(
        label="absent-governance-context",
        client=lambda: FixedClient(ALLOW),
        context=None,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=GovernanceContextError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
    ),
    Scenario(
        label="a-client-that-raises",
        client=RaisingClient,
        context=THREADED,
        classify=read_only,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=PolicyViolation,
        downstream_calls=0,
        records=1,
        decision="deny",
        interrupts=0,
    ),
    Scenario(
        label="a-classification-that-changes-after-the-tool-is-installed",
        client=SideEffectSensitiveClient,
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=ToolGovernanceError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
        resolvers=drifting_classification,
        mutate=reclassify,
    ),
    Scenario(
        label="a-contract-binding-that-changes-after-the-tool-is-installed",
        client=ContractSensitiveClient,
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=ToolGovernanceError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
        resolvers=drifting_contract,
        mutate=rebind_contract,
    ),
    Scenario(
        label="capabilities-that-change-after-the-tool-is-installed",
        client=CapabilitySensitiveClient,
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=ToolGovernanceError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
        resolvers=drifting_capabilities,
        mutate=require_more_capabilities,
    ),
    Scenario(
        label="approval-that-changes-after-the-tool-is-installed",
        client=ApprovalSensitiveClient,
        context=THREADED,
        classify=None,
        unknown_side_effect=UnknownSideEffectPolicy.DENY,
        error=ToolGovernanceError,
        downstream_calls=0,
        records=0,
        decision=None,
        interrupts=0,
        resolvers=drifting_approval,
        mutate=require_approval,
    ),
)
"""The one scenario table. Both drivers below consume exactly this structure."""


@dataclasses.dataclass(frozen=True, slots=True)
class Outcome:
    """Everything one surface did with one scenario, in a form the other's compares to.

    Attributes:
        error: The exception's exact class, or ``None``. The class, not the fact
            that something raised: two surfaces raising different subclasses of
            ``ToolGovernanceError`` for one condition is exactly the drift R8
            forbids.
        result: What the call returned when it proceeded.
        downstream_calls: How many times the downstream actually ran.
        audit: Every emitted record's projection, in emission order. A tuple, so
            "nothing was recorded" is itself a comparable outcome.
        interrupts: Every payload the pause seam received, in order.
    """

    error: type[Exception] | None
    result: Any
    downstream_calls: int
    audit: tuple[dict[str, Any], ...]
    interrupts: tuple[dict[str, Any], ...]


def _projection(record: NodeAuditRecord) -> dict[str, Any]:
    """Render one audit record as the comparable facts it asserts."""
    return record.model_dump(mode="json", exclude=_VOLATILE_FIELDS)


def _outcome(
    invoke: Callable[[], Any],
    downstream: Body | Handler,
    audit: RecordingSubmitter,
    interrupt: RecordingInterrupt,
) -> Outcome:
    """Run one governed call and collect everything the surfaces are compared on."""
    error: type[Exception] | None = None
    result: Any = None
    try:
        result = invoke()
    except Exception as raised:
        error = type(raised)
    return Outcome(
        error=error,
        result=result,
        downstream_calls=downstream.calls,
        audit=tuple(_projection(record) for record in audit.records),
        interrupts=tuple(dict(payload) for payload in interrupt.payloads),
    )


def _without_call_identity(outcome: Outcome) -> Outcome:
    """Compare shared semantics while preserving middleware-only call identity separately."""
    return dataclasses.replace(
        outcome,
        interrupts=tuple(
            {key: value for key, value in payload.items() if key != "tool_call_id"}
            for payload in outcome.interrupts
        ),
    )


def _seams(
    scenario: Scenario,
    audit: RecordingSubmitter,
    interrupt: RecordingInterrupt,
    live: dict[str, Any],
) -> dict:
    """Render one scenario as the keyword arguments both install surfaces take.

    The two surfaces accept the same seam names deliberately, so a scenario is
    installed identically on each and nothing about the *installation* can
    account for a difference in outcome.

    *live* is this driver's own cell, so a resolver one surface reads can never
    be the one the other surface mutated.
    """
    directory = tempfile.TemporaryDirectory()
    _LIFECYCLE_DIRS.append(directory)
    seams = {
        "context": scenario.context,
        "client": scenario.client(),
        "unknown_side_effect": scenario.unknown_side_effect,
        "audit": audit,
        "interrupt": interrupt,
        "approval_lifecycle": SQLiteApprovalRepository(f"{directory.name}/approvals.sqlite3"),
        "side_effect": scenario.classify,
    }
    if scenario.resolvers is not None:
        seams.update(scenario.resolvers(live))
    return seams


_LIFECYCLE_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def _staged(scenario: Scenario, live: dict[str, Any]) -> None:
    """Apply the scenario's change to *live*, between installing the tool and calling it."""
    if scenario.mutate is not None:
        scenario.mutate(live)


def drive_wrapper(scenario: Scenario, tool: Any, body: Body) -> Outcome:
    """Drive *scenario* through ``govern_tools``, invoking the governed twin."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    [governed] = govern_tools([tool], **_seams(scenario, audit, interrupt, live))
    _staged(scenario, live)
    return _outcome(lambda: governed.invoke({"query": "cats"}), body, audit, interrupt)


def drive_middleware(scenario: Scenario, tool: Any, body: Body) -> Outcome:
    """Drive *scenario* through ``ZerothMiddleware.wrap_tool_call``.

    The downstream counter is the *body*, not the handler. The handler now runs
    on every verdict -- the decision happens inside it, in the governed twin the
    middleware substituted -- so a handler count would read ``1`` for a denial and
    compare unequal to the wrapper surface for a reason that is not a divergence.
    Counting the tool function is the same measurement ``drive_wrapper`` makes.
    """
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    guard = ZerothMiddleware(**_seams(scenario, audit, interrupt, live), expected_tools=(tool,))
    _staged(scenario, live)
    request = build_request(tool)
    return _outcome(lambda: guard.wrap_tool_call(request, Handler()), body, audit, interrupt)


def drive_wrapper_async(scenario: Scenario, tool: Any, body: Body) -> Outcome:
    """Drive *scenario* through ``govern_tools``, awaiting the governed twin."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    [governed] = govern_tools([tool], **_seams(scenario, audit, interrupt, live))
    _staged(scenario, live)
    return _outcome(
        lambda: asyncio.run(governed.ainvoke({"query": "cats"})), body, audit, interrupt
    )


def drive_middleware_async(scenario: Scenario, tool: Any, body: Body) -> Outcome:
    """Drive *scenario* through ``ZerothMiddleware.awrap_tool_call``."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    guard = ZerothMiddleware(**_seams(scenario, audit, interrupt, live), expected_tools=(tool,))
    _staged(scenario, live)
    request = build_request(tool)
    return _outcome(
        lambda: asyncio.run(guard.awrap_tool_call(request, Handler().acall)),
        body,
        audit,
        interrupt,
    )


def _assert_declared(outcome: Outcome, scenario: Scenario) -> None:
    """Check one surface's outcome against what the scenario table declared.

    Equality between the two surfaces is the parity claim; this is what stops
    both of them being identically wrong.
    """
    assert outcome.error is scenario.error
    assert outcome.result == (None if scenario.error else BODY_RESULT)
    assert outcome.downstream_calls == scenario.downstream_calls
    assert len(outcome.audit) == scenario.records
    assert len(outcome.interrupts) == scenario.interrupts
    if scenario.decision is not None:
        [record] = outcome.audit
        assert record["execution_metadata"]["decision"] == scenario.decision
        assert record["tool_calls"][0]["alias"] == "search"


# --------------------------------------------------------------------------- #
# R8 -- one decision client, one exception set, one audit projection, one
# interrupt schema, driven from one table through both install surfaces.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.label)
def test_both_surfaces_decide_the_same_scenario_identically(scenario: Scenario) -> None:
    """``govern_tools`` and ``ZerothMiddleware`` produce the same outcome, field for field."""
    wrapper_body, middleware_body = Body(), Body()

    wrapper = drive_wrapper(scenario, build_tool(wrapper_body), wrapper_body)
    middleware = drive_middleware(scenario, build_tool(middleware_body), middleware_body)

    assert _without_call_identity(wrapper) == _without_call_identity(middleware)
    _assert_declared(wrapper, scenario)
    _assert_declared(middleware, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.label)
def test_both_async_surfaces_decide_the_same_scenario_identically(scenario: Scenario) -> None:
    """The async halves agree with each other over the same table.

    Neither surface carries an async enforcement core -- ``awrap_tool_call`` and
    ``_arun`` both call ``authorize_tool_call`` and then await their own
    downstream -- so this drives the same table down the other pair of paths and
    holds them to the same equality.
    """
    wrapper_body, middleware_body = Body(), Body()

    wrapper = drive_wrapper_async(scenario, build_async_tool(wrapper_body), wrapper_body)
    middleware = drive_middleware_async(
        scenario, build_async_tool(middleware_body), middleware_body
    )

    assert _without_call_identity(wrapper) == _without_call_identity(middleware)
    _assert_declared(wrapper, scenario)
    _assert_declared(middleware, scenario)


def test_the_sync_and_async_paths_of_one_surface_agree_too() -> None:
    """A scenario decided sync and async records the same thing on the same surface.

    The cross-surface tests above would still pass if *both* surfaces drifted
    between their own sync and async halves together. This pins the fourth edge
    of the square on the wrapper surface, where the two halves are separate
    methods (``_run`` and ``_arun``) rather than one shared call.

    Both halves are driven against the *same* tool -- one built with both a
    ``func`` and a ``coroutine`` -- because identity is bound to the code a tool
    runs, so a sync-only and an async-only tool differ by construction and would
    make this compare the tools rather than the paths. The two constructions get
    their own ``Body`` so a call count cannot leak between them; the
    implementation is identical, which is why their identities are.
    """
    scenario = next(item for item in SCENARIOS if item.label == "deny")
    sync_body, async_body = Body(), Body()

    sync = drive_wrapper(scenario, build_dual_tool(sync_body), sync_body)
    asynchronous = drive_wrapper_async(scenario, build_dual_tool(async_body), async_body)

    assert sync == asynchronous


def test_the_parity_table_covers_every_required_scenario() -> None:
    """The table is the deliverable, so its coverage is asserted rather than assumed."""
    labels = {scenario.label for scenario in SCENARIOS}
    assert labels == {
        "allow",
        "deny",
        "require-approval",
        "require-approval-without-a-thread",
        "unknown-side-effect-denied",
        "unknown-side-effect-admitted-by-the-named-opt-in",
        "absent-governance-context",
        "a-client-that-raises",
        "a-classification-that-changes-after-the-tool-is-installed",
        "a-contract-binding-that-changes-after-the-tool-is-installed",
        "capabilities-that-change-after-the-tool-is-installed",
        "approval-that-changes-after-the-tool-is-installed",
    }


def test_an_approval_payload_is_the_same_schema_on_both_surfaces() -> None:
    """The interrupt payload is one schema, so a resumer cannot need to know the surface.

    ``test_both_surfaces_decide_the_same_scenario_identically`` already compares
    the payloads for equality; this names the keys, so a field silently dropped
    from *both* surfaces at once is still caught.
    """
    scenario = next(item for item in SCENARIOS if item.label == "require-approval")
    wrapper_body, middleware_body = Body(), Body()

    [payload] = drive_wrapper(scenario, build_tool(wrapper_body), wrapper_body).interrupts
    [twin] = drive_middleware(scenario, build_tool(middleware_body), middleware_body).interrupts

    assert {key: value for key, value in payload.items() if key != "tool_call_id"} == {
        key: value for key, value in twin.items() if key != "tool_call_id"
    }
    assert payload["tool_call_id"] is None
    assert twin["tool_call_id"] == "call-1"
    assert set(payload) == {
        "version",
        "kind",
        "approval_ref",
        "tenant_id",
        "principal_id",
        "run_id",
        "thread_id",
        "correlation_id",
        "tool_name",
        "tool_fingerprint",
        "tool_call_id",
        "argument_fingerprint",
        "contract_ref",
        "side_effect",
        "reason_code",
    }
    assert payload["kind"] == "tool_approval"
    assert payload["approval_ref"] == "approval-7"


# --------------------------------------------------------------------------- #
# R-1 / R8 -- the decided arguments are the executed arguments, on both surfaces.
#
# The table above stays green on any implementation, because every scenario in it
# calls ``{"query": "cats"}`` against a ``str`` field: nothing is coerced, nothing
# is defaulted, and the raw model arguments happen to equal the validated ones. So
# it could not see the finding this section exists for -- a middleware that decided
# from ``request.tool_call["args"]`` authorized the *string* ``"7"`` while the body
# ran on the *integer* ``7``.
#
# Each scenario below therefore records two mappings for each surface: what the
# decision client was handed, and what the tool function actually received. The
# assertions are that the two are equal on each surface, that the surfaces agree,
# and -- for coercion and defaulting -- that the decided mapping is *not* the raw
# one, so a regression that reverts to pre-validation arguments fails here rather
# than passing for the old reason.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class ArgumentRecordingClient:
    """A permitting client that keeps the arguments of every action it decided."""

    seen: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Record what policy was shown, then allow."""
        self.seen.append(dict(action.arguments))
        return ALLOW


def build_coercing_tool() -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool whose one field is an ``int``, so a JSON string is coerced into it.

    The auditor's reproduction, as a tool: the model emits ``"7"`` and
    ``BaseTool`` hands the body ``7``.
    """
    observed: list[dict[str, Any]] = []

    class Arguments(BaseModel):
        """One integer, which a model's JSON string has to be coerced into."""

        count: int

    def _run(count: int) -> str:
        observed.append({"count": count})
        return BODY_RESULT

    tool = StructuredTool.from_function(
        func=_run, name="search", description="search for things.", args_schema=Arguments
    )
    return tool, observed


def build_defaulting_tool() -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool with a defaulted field the model never mentions.

    A policy that decides from the raw call never sees ``limit`` at all, so it
    cannot deny on it -- the argument arrives at the body having been through no
    gate whatsoever.
    """
    observed: list[dict[str, Any]] = []

    class Arguments(BaseModel):
        """One supplied field and one the schema fills in."""

        query: str
        limit: int = 5

    def _run(query: str, limit: int = 5) -> str:
        observed.append({"query": query, "limit": limit})
        return BODY_RESULT

    tool = StructuredTool.from_function(
        func=_run, name="search", description="search for things.", args_schema=Arguments
    )
    return tool, observed


def build_stateful_validator_tool() -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool whose validator answers differently every time it runs.

    This is the case that separates "validates before deciding" from "validates
    exactly once before deciding". A surface that parsed the call, decided, and
    then handed the parsed values back through the delegate's *public* entry point
    would run this validator twice: policy would see ``n-1`` and the body ``n-2``.
    """
    observed: list[dict[str, Any]] = []
    validations: list[int] = []

    class Arguments(BaseModel):
        """One field whose validation consumes a counter it cannot un-consume."""

        nonce: str

        @field_validator("nonce")
        @classmethod
        def _consume(cls, value: str) -> str:
            """Stamp the value with how many validations have happened."""
            validations.append(1)
            return f"{value}-{len(validations)}"

    def _run(nonce: str) -> str:
        observed.append({"nonce": nonce})
        return BODY_RESULT

    tool = StructuredTool.from_function(
        func=_run, name="search", description="search for things.", args_schema=Arguments
    )
    return tool, observed


@dataclasses.dataclass(frozen=True, slots=True)
class ArgumentScenario:
    """One call whose raw arguments differ from the ones the body will receive.

    Attributes:
        label: The scenario's name, used as the parametrization id.
        build: Builds a fresh tool and the list its body records into. A factory,
            so each surface is driven against state of its own -- the stateful
            validator would otherwise carry its counter across the two.
        raw: The arguments as the model emits them, before any validation.
        decided: What both the policy and the body must see. Written out rather
            than derived, so a change in what ``BaseTool`` does to a call is a
            test failure and not a silently updated expectation.
        differs: Whether *decided* must not equal *raw*. ``True`` for the cases
            that reproduce the finding; the stateful one is exempt only because
            its expectation already pins the exact validation count.
    """

    label: str
    build: Callable[[], tuple[StructuredTool, list[dict[str, Any]]]]
    raw: dict[str, Any]
    decided: dict[str, Any]
    differs: bool = True


ARGUMENT_SCENARIOS = (
    ArgumentScenario(
        label="a-string-coerced-into-an-int-field",
        build=build_coercing_tool,
        raw={"count": "7"},
        decided={"count": 7},
    ),
    ArgumentScenario(
        label="a-default-the-model-never-supplied",
        build=build_defaulting_tool,
        raw={"query": "cats"},
        decided={"query": "cats", "limit": 5},
    ),
    ArgumentScenario(
        label="a-validator-that-answers-differently-every-time-it-runs",
        build=build_stateful_validator_tool,
        raw={"nonce": "n"},
        decided={"nonce": "n-1"},
    ),
)


def _wrapper_arguments(
    scenario: ArgumentScenario,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one scenario through ``govern_tools``, returning what policy and the body saw."""
    tool, observed = scenario.build()
    client = ArgumentRecordingClient()
    [governed] = govern_tools([tool], context=THREADED, client=client, side_effect=read_only)
    governed.invoke(dict(scenario.raw))
    [decided] = client.seen
    [executed] = observed
    return decided, executed


def _middleware_arguments(
    scenario: ArgumentScenario,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one scenario through ``ZerothMiddleware``, returning the same two mappings."""
    tool, observed = scenario.build()
    client = ArgumentRecordingClient()
    guard = ZerothMiddleware(context=THREADED, client=client, side_effect=read_only)
    request = ToolCallRequest(
        tool_call={
            "name": tool.name,
            "args": dict(scenario.raw),
            "id": "call-1",
            "type": "tool_call",
        },
        tool=tool,
        state={},
        runtime=None,
    )
    guard.wrap_tool_call(request, Handler())
    [decided] = client.seen
    [executed] = observed
    return decided, executed


@pytest.mark.parametrize("scenario", ARGUMENT_SCENARIOS, ids=lambda item: item.label)
def test_both_surfaces_decide_the_arguments_the_body_receives(
    scenario: ArgumentScenario,
) -> None:
    """The authorized call is the executed call, whatever validation did to it."""
    wrapper_decided, wrapper_executed = _wrapper_arguments(scenario)
    middleware_decided, middleware_executed = _middleware_arguments(scenario)

    # The finding, stated directly: what policy saw is what ran.
    assert wrapper_decided == wrapper_executed
    assert middleware_decided == middleware_executed
    # R8: and both surfaces saw the same thing.
    assert wrapper_decided == middleware_decided
    assert wrapper_decided == scenario.decided
    if scenario.differs:
        # Without this the test would pass on the broken implementation too,
        # because the raw and validated mappings would happen to be equal.
        assert scenario.decided != scenario.raw


@pytest.mark.parametrize("scenario", ARGUMENT_SCENARIOS, ids=lambda item: item.label)
def test_the_middleware_never_decides_the_raw_model_arguments(
    scenario: ArgumentScenario,
) -> None:
    """The regression guard, aimed at the middleware surface specifically.

    ``ZerothMiddleware`` used to build its action from ``request.tool_call["args"]``
    -- the mapping the model produced, before ``BaseTool`` touched it. This asserts
    the negative directly, so reverting the substitution fails here even if some
    later change made the positive assertion above pass for another reason.
    """
    decided, _executed = _middleware_arguments(scenario)

    assert decided != scenario.raw


def test_the_argument_parity_table_covers_every_case_the_audit_named() -> None:
    """Coercion, defaulting and a stateful validator, named rather than assumed."""
    assert {scenario.label for scenario in ARGUMENT_SCENARIOS} == {
        "a-string-coerced-into-an-int-field",
        "a-default-the-model-never-supplied",
        "a-validator-that-answers-differently-every-time-it-runs",
    }


# --------------------------------------------------------------------------- #
# R-1's boundary -- an argument ``ToolNode`` injects is decided like any other.
#
# The section above needs no agent: it hands the tool its arguments directly. That
# cannot see an *injected* argument at all, because nothing in the test injects
# one -- ``ToolNode`` resolves ``InjectedState`` / ``InjectedStore`` /
# ``InjectedToolCallId`` itself, and on the middleware surface it does so *after*
# the chain has returned. So every scenario below is driven through a real
# ``create_agent`` invocation on both surfaces.
#
# What this pins is a boundary, not a refusal. An injected argument is projected
# by exactly the rule every other argument is projected by -- the canonical
# handlers in ``_tool_normalize`` -- so its *value* decides the outcome:
#
#   * the whole graph state holds ``BaseMessage`` objects, which are not a
#     representable type, so the call is refused before any policy is consulted;
#   * a narrowed state slice that is a ``str`` is representable, so policy sees it
#     and can deny on it;
#   * a tool call id is a ``str`` too, so the same holds.
#
# The refusal row is the one that changed in 0.13.13, and the two allowed rows are
# what make it survivable: they are the shape a user narrows *to*. Documented in
# ``docs/how-to/cookbook/govern-langgraph-tools.md`` under "What governance
# refuses to wrap"; if either allowed row ever starts refusing, that workaround is
# gone and the doc is wrong.
#
# Eliding an injected argument instead is deliberately not an option, on either
# surface: see the module docstring of ``_tool_normalize`` -- a policy that denies
# ``path="/etc/shadow"`` must not be handed a call with the path removed.
# --------------------------------------------------------------------------- #

RAW_INJECTED_CALL = {"query": "cats"}
"""What the model emits for every injected scenario: the model-supplied part only.

An injected argument is absent here on purpose. That is the definition of one --
the model never supplies it -- and it is why deciding from the raw tool call could
never have shown policy the value the body was about to run on.
"""


class SliceState(TypedDict):
    """A graph state with one field that *is* canonically representable.

    A subclass of ``AgentState`` cannot be used here: it carries managed channels
    that ``create_agent`` refuses in an input schema. A plain ``TypedDict`` with
    ``messages`` is the narrowest state that both drives an agent and holds a
    field a tool can inject a ``str`` out of.
    """

    messages: Annotated[list, add_messages]
    user_id: str


def _one_body_tool(
    sync_body: Callable[..., str], async_body: Callable[..., str], *, is_async: bool
) -> StructuredTool:
    """Build the tool from exactly *one* of the two bodies.

    Which governance entry point runs is decided by the driver, not by the tool:
    ``ainvoke`` reaches ``awrap_tool_call`` even for a tool whose only body is
    sync, and
    :func:`test_each_driver_reaches_governance_through_its_own_entry_point`
    asserts that separately. What a sync body would change is everything *below*
    governance -- ``StructuredTool`` serves the coroutine by running that function
    in a threadpool -- so an "async" row built with ``func`` would prove the async
    wrapper was entered and nothing about what it then drove. Handing
    ``from_function`` only ``coroutine`` leaves no sync path to fall back to, so
    the row is async the whole way to the body.
    """
    if is_async:
        return StructuredTool.from_function(
            coroutine=async_body, name="search", description="search for things."
        )
    return StructuredTool.from_function(
        func=sync_body, name="search", description="search for things."
    )


def build_whole_state_tool(*, is_async: bool) -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool that injects the entire graph state.

    A refused row. ``ToolNode`` hands this the whole state mapping, whose
    ``messages`` are ``BaseMessage`` objects -- not one of the exact types the
    canonical projection accepts -- so normalization refuses the call and the body
    never runs.
    """
    observed: list[dict[str, Any]] = []

    def _run(query: str, state: Annotated[dict, InjectedState]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    async def _arun(query: str, state: Annotated[dict, InjectedState]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    return _one_body_tool(_run, _arun, is_async=is_async), observed


def build_store_tool(*, is_async: bool) -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool that injects the graph's store handle, annotated by the class.

    The row the cookbook's table asserted and no test ever executed. ``BaseStore``
    has no JSON schema, so tool identity falls back to describing the declared
    fields -- and there the *class* ``InjectedStore`` reprs stably, so identity
    succeeds and the call reaches the argument projection. The injected value is a
    live ``BaseStore`` instance, which is not canonically representable, so the
    call is refused exactly where the whole-state row is refused.
    """
    observed: list[dict[str, Any]] = []

    def _run(query: str, store: Annotated[BaseStore, InjectedStore]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    async def _arun(query: str, store: Annotated[BaseStore, InjectedStore]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    return _one_body_tool(_run, _arun, is_async=is_async), observed


def build_store_instance_tool(*, is_async: bool) -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build the same tool with ``InjectedStore()`` -- an annotation *instance*.

    Refused too, but one stage earlier and for a different reason, which is why it
    is a row of its own rather than a spelling of the one above.

    ``InjectedStore()`` inherits ``object.__repr__``, so it renders with a memory
    address. That only matters because ``BaseStore`` has no JSON schema: identity
    falls back to ``_model_field_material``, which reprs the field's *metadata*,
    and an address-bearing repr is refused as an unstable identity. The narrowed
    state slice above is an annotation instance too and is governed fine -- its
    declared type is ``str``, so the JSON schema builds and the metadata is never
    rendered. The trigger is the undescribable *type*, not the injection.
    """
    observed: list[dict[str, Any]] = []

    def _run(query: str, store: Annotated[BaseStore, InjectedStore()]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    async def _arun(query: str, store: Annotated[BaseStore, InjectedStore()]) -> str:
        observed.append({"query": query})
        return BODY_RESULT

    return _one_body_tool(_run, _arun, is_async=is_async), observed


def build_state_slice_tool(*, is_async: bool) -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool that injects one representable field out of the state.

    The route the cookbook prescribes, as a test: narrowing the injection to the
    slice the tool actually needs makes the argument representable, and a
    representable injected argument is one policy is shown and can deny on.
    """
    observed: list[dict[str, Any]] = []

    def _run(query: str, user_id: Annotated[str, InjectedState("user_id")]) -> str:
        observed.append({"query": query, "user_id": user_id})
        return BODY_RESULT

    async def _arun(query: str, user_id: Annotated[str, InjectedState("user_id")]) -> str:
        observed.append({"query": query, "user_id": user_id})
        return BODY_RESULT

    return _one_body_tool(_run, _arun, is_async=is_async), observed


def build_tool_call_id_tool(*, is_async: bool) -> tuple[StructuredTool, list[dict[str, Any]]]:
    """Build a tool that injects its own tool call id.

    Also representable, and worth pinning separately: ``InjectedToolCallId`` is the
    injected argument most tools that return commands or ``ToolMessage`` values
    declare, so a blanket refusal of injected arguments would have taken it out.
    """
    observed: list[dict[str, Any]] = []

    def _run(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append({"query": query, "tool_call_id": tool_call_id})
        return BODY_RESULT

    async def _arun(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append({"query": query, "tool_call_id": tool_call_id})
        return BODY_RESULT

    return _one_body_tool(_run, _arun, is_async=is_async), observed


@dataclasses.dataclass(frozen=True, slots=True)
class InjectedScenario:
    """One call carrying an argument the model did not supply.

    Attributes:
        label: The scenario's name, used as the parametrization id.
        build: Builds a fresh tool and the list its body records into, for the
            sync or the async driver. A factory, so each surface and each driver
            is driven against state of its own.
        decided: What policy and the body must both see, or ``None`` when the call
            must be refused before either happens.
        refusal: The exact class a refused row raises. Written down per row
            because the two store rows are refused at different stages and a test
            that accepted any ``ToolGovernanceError`` would not notice one of them
            turning into the other.
        state: Extra state the agent is invoked with, beyond ``messages``.
        state_schema: The graph state schema, when the scenario needs a field the
            default one does not declare.
        store: Whether the agent is compiled with a store. Only an
            ``InjectedStore`` row needs one, and without it LangGraph refuses the
            call itself -- see
            :func:`test_an_injected_store_without_a_store_is_refused_by_langgraph_first`.
    """

    label: str
    build: Callable[..., tuple[StructuredTool, list[dict[str, Any]]]]
    decided: dict[str, Any] | None
    refusal: type[BaseException] | None = None
    state: dict[str, Any] = dataclasses.field(default_factory=dict)
    state_schema: Any = None
    store: bool = False


INJECTED_SCENARIOS = (
    InjectedScenario(
        label="the-whole-graph-state-is-not-representable",
        build=build_whole_state_tool,
        decided=None,
        refusal=ToolGovernanceError,
    ),
    InjectedScenario(
        label="an-injected-store-handle-is-not-representable",
        build=build_store_tool,
        decided=None,
        refusal=ToolGovernanceError,
        store=True,
    ),
    InjectedScenario(
        label="an-injected-store-annotation-instance-has-no-stable-identity",
        build=build_store_instance_tool,
        decided=None,
        refusal=UnstableToolIdentityError,
        store=True,
    ),
    InjectedScenario(
        label="a-narrowed-state-slice-is-representable",
        build=build_state_slice_tool,
        decided={"query": "cats", "user_id": "u-1"},
        state={"user_id": "u-1"},
        state_schema=SliceState,
    ),
    InjectedScenario(
        label="an-injected-tool-call-id-is-representable",
        build=build_tool_call_id_tool,
        decided={"query": "cats", "tool_call_id": TOOL_CALL_ID},
    ),
)

INJECTED_DRIVERS = (False, True)
"""Whether the agent is driven through ``ainvoke`` rather than ``invoke``.

Both, for every row. ``ZerothMiddleware`` carries ``wrap_tool_call`` and
``awrap_tool_call`` as separate implementations and ``govern_tools`` builds a
separate async wrapper, so a projection rule proven on one driver is not proven
on the other.
"""


def _drive_injected(
    scenario: InjectedScenario,
    install: Callable[[StructuredTool, Any], dict[str, Any]],
    *,
    is_async: bool,
) -> tuple[type[BaseException] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one injected scenario through whichever surface *install* configures.

    ``create_agent`` is inside the ``try`` on purpose: ``govern_tools`` describes
    a tool's identity when it wraps it, so the annotation-instance row is refused
    at *install* on that surface and at the first call on the middleware's. Both
    are the same fail-closed refusal of the same tool, and a harness that only
    guarded the call would let one of them escape as an error.

    Args:
        scenario: The scenario to drive.
        install: Turns the tool and the decision client into the ``create_agent``
            keyword arguments for one surface -- which is the *only* thing that
            differs between the two drivers.
        is_async: Drive through ``ainvoke`` rather than ``invoke``.

    Returns:
        The refusal's exact class or ``None``, what policy was shown, and what the
        body received.
    """
    tool, observed = scenario.build(is_async=is_async)
    client = ArgumentRecordingClient()
    schema = {} if scenario.state_schema is None else {"state_schema": scenario.state_schema}
    store = {"store": InMemoryStore()} if scenario.store else {}
    invocation: dict[str, Any] = {"messages": [HumanMessage("hi")], **scenario.state}
    try:
        agent = create_agent(
            scripted_model("search", dict(RAW_INJECTED_CALL)),
            **install(tool, client),
            **schema,
            **store,
        )
        if is_async:
            asyncio.run(agent.ainvoke(invocation))
        else:
            agent.invoke(invocation)
    except ToolGovernanceError as error:
        return type(error), client.seen, observed
    return None, client.seen, observed


def _wrapper_install(tool: StructuredTool, client: Any) -> dict[str, Any]:
    """Install the tool through ``govern_tools``: the wrapper *is* the agent's tool."""
    return {"tools": govern_tools([tool], context=THREADED, client=client, side_effect=read_only)}


def _middleware_install(tool: StructuredTool, client: Any) -> dict[str, Any]:
    """Install the tool raw and govern it with ``ZerothMiddleware`` instead."""
    return {
        "tools": [tool],
        "middleware": [ZerothMiddleware(context=THREADED, client=client, side_effect=read_only)],
    }


@pytest.mark.parametrize("is_async", INJECTED_DRIVERS, ids=("sync", "async"))
@pytest.mark.parametrize("scenario", INJECTED_SCENARIOS, ids=lambda item: item.label)
def test_both_surfaces_decide_an_injected_argument_identically(
    scenario: InjectedScenario, is_async: bool
) -> None:
    """An injected argument is projected by one rule, and both surfaces apply it.

    The equality comes first, as everywhere in this module: whether an injected
    argument is representable is a governance fact, and a surface that refused
    where the other allowed would be an R8 break regardless of which answer is the
    right one.
    """
    wrapper = _drive_injected(scenario, _wrapper_install, is_async=is_async)
    middleware = _drive_injected(scenario, _middleware_install, is_async=is_async)

    # R8: one projection, so one answer.
    assert wrapper == middleware

    error, decided, executed = wrapper
    if scenario.decided is None:
        # Refused before a policy is consulted -- so an argument governance cannot
        # describe or represent is never half-decided.
        assert error is scenario.refusal
        assert (decided, executed) == ([], [])
    else:
        assert error is None
        # R-1 holds across injection too: policy saw exactly what the body ran on,
        # including the argument the model never supplied.
        assert decided == [scenario.decided]
        assert executed == [scenario.decided]


def test_the_injected_parity_table_covers_refused_and_representable_shapes() -> None:
    """Every row is named, so deleting one cannot pass quietly.

    Both directions and both refusal *stages*: the two store rows differ only in
    whether the annotation is the class or an instance of it, and they are refused
    by different rules, so losing either one silently would leave the cookbook's
    store row asserting something no test runs.
    """
    assert {scenario.label for scenario in INJECTED_SCENARIOS} == {
        "the-whole-graph-state-is-not-representable",
        "an-injected-store-handle-is-not-representable",
        "an-injected-store-annotation-instance-has-no-stable-identity",
        "a-narrowed-state-slice-is-representable",
        "an-injected-tool-call-id-is-representable",
    }
    refused = [scenario for scenario in INJECTED_SCENARIOS if scenario.decided is None]
    assert len(refused) == 3
    assert {scenario.refusal for scenario in refused} == {
        ToolGovernanceError,
        UnstableToolIdentityError,
    }
    assert all(scenario.refusal is None for scenario in INJECTED_SCENARIOS if scenario.decided)


@pytest.mark.parametrize("scenario", INJECTED_SCENARIOS, ids=lambda item: item.label)
def test_an_async_injected_tool_has_no_sync_body_to_fall_back_on(
    scenario: InjectedScenario,
) -> None:
    """One of the two controls that keep the async rows from going vacuous.

    Entering ``awrap_tool_call`` is not by itself proof that an async row ran
    anything asynchronously: a sync ``func`` still gets there, and
    ``StructuredTool`` then runs it in a threadpool. The async rows are therefore
    built from ``coroutine`` alone, and this asserts the two builds really are
    disjoint rather than trusting :func:`_one_body_tool` to have made them so.

    What that absence *means* is the other control, kept separate because it is a
    fact about ``StructuredTool`` rather than about this table:
    :func:`test_a_coroutine_only_tool_cannot_serve_a_sync_invocation`. The two
    together are what make an async row's governed execution async end to end.
    """
    asynchronous, _ = scenario.build(is_async=True)
    synchronous, _ = scenario.build(is_async=False)

    assert (asynchronous.func, synchronous.coroutine) == (None, None)
    assert asynchronous.coroutine is not None
    assert synchronous.func is not None


def test_a_coroutine_only_tool_cannot_serve_a_sync_invocation() -> None:
    """The other half: a tool with no ``func`` has no sync path to fall back to.

    Asserted on a plain tool with no injected argument, because an injected one
    cannot be invoked directly at all -- ``ToolNode`` is what resolves it, and
    ``BaseTool`` rejects the call for the missing argument before the body's
    absence could ever be reached. Together with the row-by-row build check above,
    this is what makes an async row that ran its body evidence that the async path
    ran it.
    """

    async def _arun(query: str) -> str:
        return BODY_RESULT

    tool = StructuredTool.from_function(
        coroutine=_arun, name="search", description="search for things."
    )

    with pytest.raises(NotImplementedError):
        tool.invoke({"query": "cats"})
    assert asyncio.run(tool.ainvoke({"query": "cats"})) == BODY_RESULT


@pytest.mark.parametrize("is_async", INJECTED_DRIVERS, ids=("sync", "async"))
@pytest.mark.parametrize("scenario", INJECTED_SCENARIOS, ids=lambda item: item.label)
def test_each_driver_reaches_governance_through_its_own_entry_point(
    scenario: InjectedScenario, is_async: bool
) -> None:
    """The async rows enter ``awrap_tool_call``, including the refused ones.

    The build-shape controls above say what the tool could execute; this says
    which wrapper governance actually ran, which is the half that matters for a
    *refused* row. A refused row never reaches its body at all -- governance
    refuses first -- so no control that reasons from the body can speak for it.
    Asserted for every row and both drivers, so an async row that quietly started
    being governed by the sync wrapper fails here.
    """
    took: list[str] = []

    class Recording(ZerothMiddleware):
        """Records which of the two wrappers ran, then delegates unchanged."""

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            """Delegate, having named the sync entry point."""
            took.append("sync")
            return super().wrap_tool_call(request, handler)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            """Delegate, having named the async entry point."""
            took.append("async")
            return await super().awrap_tool_call(request, handler)

    middleware = Recording(context=THREADED, client=ArgumentRecordingClient())
    tool, _ = scenario.build(is_async=is_async)
    store = {"store": InMemoryStore()} if scenario.store else {}
    schema = {} if scenario.state_schema is None else {"state_schema": scenario.state_schema}
    agent = create_agent(
        scripted_model("search", dict(RAW_INJECTED_CALL)),
        tools=[tool],
        middleware=[middleware],
        **schema,
        **store,
    )
    invocation: dict[str, Any] = {"messages": [HumanMessage("hi")], **scenario.state}

    try:
        if is_async:
            asyncio.run(agent.ainvoke(invocation))
        else:
            agent.invoke(invocation)
    except ToolGovernanceError:
        pass

    assert took == ["async" if is_async else "sync"]


def test_an_injected_store_without_a_store_is_refused_by_langgraph_first() -> None:
    """Scope the store rows: an uncompiled store never reaches governance at all.

    Worth pinning because the alternative would quietly weaken the table. Had
    LangGraph injected ``None`` for a missing store, ``None`` *is* canonically
    representable -- the call would be governed rather than refused, and the
    cookbook's store row would be wrong for every graph compiled without one.
    LangGraph refuses first instead, so the row is exactly as narrow as it reads.
    """
    tool, observed = build_store_tool(is_async=False)
    client = ArgumentRecordingClient()
    agent = create_agent(
        scripted_model("search", dict(RAW_INJECTED_CALL)),
        **_middleware_install(tool, client),
    )

    with pytest.raises(ValueError, match="compile your graph with a store"):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert (client.seen, observed) == ([], [])


def test_the_two_surfaces_refuse_an_unstable_annotation_at_different_stages() -> None:
    """Where the annotation-instance row is refused, which is not the same on both.

    ``govern_tools`` derives identity when it wraps, so it refuses while the agent
    is still being built; ``ZerothMiddleware`` substitutes its governed twin per
    call, so it refuses on the first one. Both are fail-closed and both refuse the
    same tool, which is what
    :func:`test_both_surfaces_decide_an_injected_argument_identically` asserts --
    this pins the timing the cookbook describes, and it is deliberately *not* part
    of that equality: when a surface governs is a property of the surface, not a
    divergence in what governance decided.
    """
    client = ArgumentRecordingClient()
    tool, _ = build_store_instance_tool(is_async=False)

    with pytest.raises(UnstableToolIdentityError):
        govern_tools([tool], context=THREADED, client=client, side_effect=read_only)

    tool, observed = build_store_instance_tool(is_async=False)
    agent = create_agent(
        scripted_model("search", dict(RAW_INJECTED_CALL)),
        **_middleware_install(tool, client),
        store=InMemoryStore(),
    )

    with pytest.raises(UnstableToolIdentityError):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert (client.seen, observed) == ([], [])
