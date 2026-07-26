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
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain.agents.middleware import ToolCallRequest
from langchain_core.tools import StructuredTool

from zeroth.governance.audit import NodeAuditRecord
from zeroth.integrations.langgraph._middleware import ZerothMiddleware
from zeroth.integrations.langgraph._tool_decisions import UnknownSideEffectPolicy
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
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
    """The downstream LangChain hands the middleware surface, counting invocations."""

    calls: int = 0

    def __call__(self, request: Any) -> Any:
        """Count this invocation and return the shared result."""
        self.calls += 1
        return BODY_RESULT

    async def acall(self, request: Any) -> Any:
        """The awaitable form of the same counting downstream."""
        await asyncio.sleep(0)
        return self(request)


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
        error=PolicyViolation,
        downstream_calls=0,
        records=1,
        decision="deny",
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
        error=PolicyViolation,
        downstream_calls=0,
        records=1,
        decision="deny",
        interrupts=0,
        resolvers=drifting_contract,
        mutate=rebind_contract,
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
    seams = {
        "context": scenario.context,
        "client": scenario.client(),
        "unknown_side_effect": scenario.unknown_side_effect,
        "audit": audit,
        "interrupt": interrupt,
        "side_effect": scenario.classify,
    }
    if scenario.resolvers is not None:
        seams.update(scenario.resolvers(live))
    return seams


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


def drive_middleware(scenario: Scenario, tool: Any) -> Outcome:
    """Drive *scenario* through ``ZerothMiddleware.wrap_tool_call``."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    guard = ZerothMiddleware(**_seams(scenario, audit, interrupt, live))
    _staged(scenario, live)
    handler = Handler()
    request = build_request(tool)
    return _outcome(lambda: guard.wrap_tool_call(request, handler), handler, audit, interrupt)


def drive_wrapper_async(scenario: Scenario, tool: Any, body: Body) -> Outcome:
    """Drive *scenario* through ``govern_tools``, awaiting the governed twin."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    [governed] = govern_tools([tool], **_seams(scenario, audit, interrupt, live))
    _staged(scenario, live)
    return _outcome(
        lambda: asyncio.run(governed.ainvoke({"query": "cats"})), body, audit, interrupt
    )


def drive_middleware_async(scenario: Scenario, tool: Any) -> Outcome:
    """Drive *scenario* through ``ZerothMiddleware.awrap_tool_call``."""
    audit, interrupt, live = RecordingSubmitter(), RecordingInterrupt(), {}
    guard = ZerothMiddleware(**_seams(scenario, audit, interrupt, live))
    _staged(scenario, live)
    handler = Handler()
    request = build_request(tool)
    return _outcome(
        lambda: asyncio.run(guard.awrap_tool_call(request, handler.acall)),
        handler,
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
    body = Body()
    tool = build_tool(body)

    wrapper = drive_wrapper(scenario, tool, body)
    middleware = drive_middleware(scenario, tool)

    assert wrapper == middleware
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
    body = Body()
    tool = build_async_tool(body)

    wrapper = drive_wrapper_async(scenario, tool, body)
    middleware = drive_middleware_async(scenario, tool)

    assert wrapper == middleware
    _assert_declared(wrapper, scenario)
    _assert_declared(middleware, scenario)


def test_the_sync_and_async_paths_of_one_surface_agree_too() -> None:
    """A scenario decided sync and async records the same thing on the same surface.

    The cross-surface tests above would still pass if *both* surfaces drifted
    between their own sync and async halves together. This pins the fourth edge
    of the square on the wrapper surface, where the two halves are separate
    methods (``_run`` and ``_arun``) rather than one shared call.
    """
    scenario = next(item for item in SCENARIOS if item.label == "deny")
    sync_body, async_body = Body(), Body()

    sync = drive_wrapper(scenario, build_tool(sync_body), sync_body)
    asynchronous = drive_wrapper_async(scenario, build_async_tool(async_body), async_body)

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
    }


def test_an_approval_payload_is_the_same_schema_on_both_surfaces() -> None:
    """The interrupt payload is one schema, so a resumer cannot need to know the surface.

    ``test_both_surfaces_decide_the_same_scenario_identically`` already compares
    the payloads for equality; this names the keys, so a field silently dropped
    from *both* surfaces at once is still caught.
    """
    scenario = next(item for item in SCENARIOS if item.label == "require-approval")
    body = Body()
    tool = build_tool(body)

    [payload] = drive_wrapper(scenario, tool, body).interrupts
    [twin] = drive_middleware(scenario, tool).interrupts

    assert payload == twin
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
        "argument_fingerprint",
        "contract_ref",
        "side_effect",
        "reason_code",
    }
    assert payload["kind"] == "tool_approval"
    assert payload["approval_ref"] == "approval-7"
