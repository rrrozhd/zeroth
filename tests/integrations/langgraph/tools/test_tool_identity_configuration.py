"""Proof that declared instance configuration is part of a tool's identity.

**The finding this file pins.** Identity covered a tool's *code* and its declared
surface, never the configuration bound onto the instance running it. So
``HttpTool(endpoint="https://good.example")`` and
``HttpTool(endpoint="https://evil.example")`` were one identity, and a policy that
authorized the first authorized the second. The same held for a factory closing
over its configuration.

**Why it could not simply be digested.** Identity is re-derived and compared on
*every* call, so digesting all bound state would make a tool that counts its own
invocations refuse its own second call. The line drawn here is a *declaration*:
the author names which fields decide identity, and only those are read by value.
Everything undeclared stays state, so
``test_a_tool_that_carries_state_keeps_its_identity_across_hundreds_of_calls``
keeps passing unchanged.

**The declaration is names, never values.** An author asserts a field list and
never a digest -- the list itself is digested alongside the values it selects, so
stripping it, faking it or renaming a field moves the identity rather than
matching it. That keeps the module's "derived, never asserted" rule intact.

Every test below holds every axis except configuration identical: same class, same
name, same description, same schema, same body. Anything less would pass for the
wrong reason.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from zeroth.integrations.langgraph._tool_configuration import (
    DECLARATION_ATTRIBUTE,
    declared_configuration_names,
    identity_configuration,
)
from zeroth.integrations.langgraph._tool_errors import UnstableToolIdentityError
from zeroth.integrations.langgraph._tool_inventory import (
    match_tool_inventory,
    record_binding_inventory,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._tool_wrappers import govern_tools

CONTEXT = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

DESCRIPTION = "fetch a path."
"""One description, shared by every tool here, so it can never be the difference."""

GOOD = "https://good.example"
EVIL = "https://evil.example"


class Allowing:
    """A decision client that allows everything, so a call reaches the body."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Allow the call."""
        return ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")


def read_only(_tool: Any) -> SideEffectClass:
    """Classify every tool read-only, so an allow is reachable."""
    return SideEffectClass.READ_ONLY


def identity_of(target: Any) -> Any:
    """Pin one tool the way the install surface pins it, and read back the identity."""
    [governed] = govern_tools([target])
    return governed.zeroth_binding.identity


def governed_call(target: Any) -> Any:
    """Govern one tool so that an allowed call actually reaches its body."""
    [governed] = govern_tools([target], context=CONTEXT, client=Allowing(), side_effect=read_only)
    return governed


class HttpTool(BaseTool):
    """The ticket's probe: one class, two endpoints, historically one identity."""

    zeroth_identity_configuration: ClassVar[tuple[str, ...]] = ("endpoint",)

    name: str = "fetch"
    description: str = DESCRIPTION
    endpoint: str

    def _run(self, path: str) -> str:
        """Fetch *path* from the configured endpoint."""
        return f"{self.endpoint}/{path}"


class UndeclaredHttpTool(BaseTool):
    """``HttpTool`` to the byte, minus the declaration."""

    name: str = "fetch"
    description: str = DESCRIPTION
    endpoint: str

    def _run(self, path: str) -> str:
        """Fetch *path* from the configured endpoint."""
        return f"{self.endpoint}/{path}"


def make_fetcher(endpoint: str) -> Any:
    """Build a tool body that closes over its configuration."""

    @identity_configuration("endpoint")
    def fetch(path: str) -> str:
        """Fetch a path."""
        return f"{endpoint}/{path}"

    return fetch


def make_undeclared_fetcher(endpoint: str) -> Any:
    """The same factory, declaring nothing."""

    def fetch(path: str) -> str:
        """Fetch a path."""
        return f"{endpoint}/{path}"

    return fetch


def structured(body: Any) -> StructuredTool:
    """Bind a body into a ``StructuredTool`` under one fixed surface."""
    return StructuredTool.from_function(func=body, name="fetch", description=DESCRIPTION)


def test_a_class_level_declaration_separates_two_differing_instance_configurations() -> None:
    """R1: the ticket's probe, closed. Same class, same body, two endpoints."""
    good = identity_of(HttpTool(endpoint=GOOD))
    evil = identity_of(HttpTool(endpoint=EVIL))

    assert good.name == evil.name
    assert good.fingerprint != evil.fingerprint


def test_the_http_tool_probe_still_collapses_when_nothing_is_declared() -> None:
    """R5: the documented fallback, asserted rather than assumed.

    Undeclared configuration keeps today's code-only identity. This is the
    decided behaviour, and it is pinned so a later change to it is a test
    failure rather than a silent policy shift.
    """
    good = identity_of(UndeclaredHttpTool(endpoint=GOOD))
    evil = identity_of(UndeclaredHttpTool(endpoint=EVIL))

    assert good.fingerprint == evil.fingerprint


def test_declaring_a_field_never_yields_the_identity_the_same_tool_had_undeclared() -> None:
    """R5: the declaration is non-forgeable in both directions.

    A substitute that drops the declaration cannot match a declaring original's
    recorded fingerprint, because the declaration is itself digested.
    """
    declared = identity_of(HttpTool(endpoint=GOOD))
    undeclared = identity_of(UndeclaredHttpTool(endpoint=GOOD))

    assert declared.name == undeclared.name
    assert declared.fingerprint != undeclared.fingerprint


def test_a_closure_declaration_separates_two_configurations_of_one_factory() -> None:
    """R2: ``make_tool(config_a)`` and ``make_tool(config_b)`` stop colliding."""
    good = identity_of(make_fetcher(GOOD))
    evil = identity_of(make_fetcher(EVIL))

    assert good.fingerprint != evil.fingerprint


def test_two_closures_of_one_factory_still_collide_when_nothing_is_declared() -> None:
    """R5: the fallback again, on the closure carrier."""
    good = identity_of(make_undeclared_fetcher(GOOD))
    evil = identity_of(make_undeclared_fetcher(EVIL))

    assert good.fingerprint == evil.fingerprint


def test_a_structured_tool_from_function_carries_a_closure_declaration() -> None:
    """R2: ``StructuredTool`` is a ``BaseTool`` whose configuration is a closure.

    The carrier is chosen per declared name, not per tool kind, which is the only
    reason this case works at all.
    """
    good = identity_of(structured(make_fetcher(GOOD)))
    evil = identity_of(structured(make_fetcher(EVIL)))

    assert good.fingerprint != evil.fingerprint


def test_a_declared_name_that_names_nothing_refuses_the_tool() -> None:
    """R3: an unresolvable declaration is an error, not an unenforced declaration."""

    class Misdeclared(BaseTool):
        """Declares a field it does not have."""

        zeroth_identity_configuration: ClassVar[tuple[str, ...]] = ("missing",)

        name: str = "fetch"
        description: str = DESCRIPTION

        def _run(self, path: str) -> str:
            """Return the path."""
            return path

    with pytest.raises(UnstableToolIdentityError):
        identity_of(Misdeclared())


def test_a_declared_value_that_cannot_be_projected_refuses_the_tool() -> None:
    """R4: a declared field fails closed rather than degrading to a type name.

    ``_bound_material`` degrades an unprojectable *undeclared* value to its type,
    which is right for a cached client nobody claimed decides identity. A field
    the author declared is the opposite case: a type name does not decide
    anything, so the tool is refused.
    """

    class Opaque:
        """Something the canonical projection cannot represent."""

    class OpaqueConfigured(BaseTool):
        """Declares a field holding an unprojectable value."""

        zeroth_identity_configuration: ClassVar[tuple[str, ...]] = ("client",)

        model_config = {"arbitrary_types_allowed": True}

        name: str = "fetch"
        description: str = DESCRIPTION
        client: Opaque

        def _run(self, path: str) -> str:
            """Return the path."""
            return path

    with pytest.raises(UnstableToolIdentityError):
        identity_of(OpaqueConfigured(client=Opaque()))


def test_every_carrier_of_a_declared_name_is_recorded_not_just_the_first() -> None:
    """R3: carriers are enumerated, because precedence pinned the wrong value.

    An attribute and a closed-over variable of the same name are two different
    values, not two spellings of one. Recording only the first meant the
    fingerprint could describe a value the body never reads.
    """

    def build() -> Any:
        endpoint = EVIL

        def fetch(path: str) -> str:
            """Fetch a path."""
            return f"{endpoint}/{path}"

        return fetch

    body = build()
    setattr(body, DECLARATION_ATTRIBUTE, ("endpoint",))
    body.endpoint = GOOD

    from zeroth.integrations.langgraph._tool_configuration import configuration_values

    assert configuration_values(("endpoint",), body, {"target": body}) == {
        "endpoint": {"attribute": GOOD, "freevar:target": EVIL}
    }


def test_a_tool_that_declares_nothing_reports_no_declared_names() -> None:
    """R5: absence is absence -- the material carries no configuration key."""
    tool = UndeclaredHttpTool(endpoint=GOOD)

    assert declared_configuration_names(tool, {}) == ()


def test_a_declaring_tool_that_carries_undeclared_state_survives_hundreds_of_calls() -> None:
    """R6: the forced trade-off is respected -- only declared fields are read.

    The tool declares its endpoint *and* counts its own invocations. The counter
    must not read as a substitution, or every long-running agent breaks.
    """
    calls = 0

    def build(endpoint: str) -> Any:
        @identity_configuration("endpoint")
        def fetch(path: str) -> str:
            """Fetch a path and count the call."""
            nonlocal calls
            calls += 1
            return f"{endpoint}/{path}:{calls}"

        return fetch

    governed = governed_call(structured(build(GOOD)))

    for index in range(1, 301):
        assert governed.invoke({"path": "a"}).endswith(f":{index}")
    assert calls == 300


def test_repeated_calls_keep_a_declared_identity_on_every_governed_surface() -> None:
    """R7: every material-construction site agrees, or the second call refuses.

    Identity is re-derived per call and compared with the pinned one. A site that
    builds material without the configuration key while another builds it with
    one is drift the wrapper itself reports, so calling twice is the whole test.
    """
    tool = governed_call(HttpTool(endpoint=GOOD))
    assert tool.invoke({"path": "a"}) == f"{GOOD}/a"
    assert tool.invoke({"path": "b"}) == f"{GOOD}/b"

    structured_tool = governed_call(structured(make_fetcher(GOOD)))
    assert structured_tool.invoke({"path": "a"}) == f"{GOOD}/a"
    assert structured_tool.invoke({"path": "b"}) == f"{GOOD}/b"

    bare = governed_call(make_fetcher(GOOD))
    assert bare(path="a") == f"{GOOD}/a"
    assert bare(path="b") == f"{GOOD}/b"


@pytest.mark.asyncio
async def test_repeated_async_calls_keep_a_declared_identity() -> None:
    """R7: the async surface re-derives identity through the same sites."""

    def build(endpoint: str) -> Any:
        @identity_configuration("endpoint")
        async def fetch(path: str) -> str:
            """Fetch a path."""
            return f"{endpoint}/{path}"

        return fetch

    governed = governed_call(
        StructuredTool.from_function(coroutine=build(GOOD), name="fetch", description=DESCRIPTION)
    )

    assert await governed.ainvoke({"path": "a"}) == f"{GOOD}/a"
    assert await governed.ainvoke({"path": "b"}) == f"{GOOD}/b"


def test_declared_configuration_changed_after_governing_refuses_the_next_call() -> None:
    """R8: reconfiguration is now a detected substitution.

    This is the whole point of the ticket: the authorization granted to the tool
    at ``https://good.example`` does not travel to the same object pointed at
    ``https://evil.example``.
    """
    tool = HttpTool(endpoint=GOOD)
    governed = governed_call(tool)
    assert governed.invoke({"path": "a"}) == f"{GOOD}/a"

    tool.endpoint = EVIL

    with pytest.raises(UnstableToolIdentityError):
        governed.invoke({"path": "b"})


def test_the_declaration_reaches_the_binding_and_the_recorded_inventory() -> None:
    """R9: an operator's recorded inventory stops matching a reconfigured tool."""
    [good] = govern_tools([HttpTool(endpoint=GOOD)])
    [evil] = govern_tools([HttpTool(endpoint=EVIL)])

    assert good.zeroth_binding.identity_configuration == ("endpoint",)

    recorded = record_binding_inventory([good.zeroth_binding])
    assert match_tool_inventory(recorded, [good.zeroth_binding.identity]).matched
    assert not match_tool_inventory(recorded, [evil.zeroth_binding.identity]).matched


def test_the_declaration_helper_is_exported_from_the_package() -> None:
    """R11: the API a tool author writes against is public."""
    import zeroth.integrations.langgraph as package

    assert "identity_configuration" in package.__all__
    assert package.identity_configuration is identity_configuration


# --------------------------------------------------------------------------- #
# Probes for the initial audit's findings (ZER18-AUD-001..005).
#
# Each one reproduces a specific way a declaration could describe something other
# than what the governed body will actually run. A declaration that pins the
# wrong value is worse than no declaration: it reports assurance it does not have.
# --------------------------------------------------------------------------- #


async def _agood(endpoint: str) -> Any:
    """Build an async body closing over its configuration."""

    @identity_configuration("endpoint")
    async def fetch(path: str) -> str:
        """Fetch a path."""
        return f"{endpoint}/{path}"

    return fetch


def _async_fetcher(endpoint: str) -> Any:
    """Build an async body closing over its configuration."""

    @identity_configuration("endpoint")
    async def fetch(path: str) -> str:
        """Fetch a path."""
        return f"{endpoint}/{path}"

    return fetch


def test_a_metadata_resolver_cannot_move_declared_configuration_mid_call() -> None:
    """ZER18-AUD-001: the resolver window is after the identity check.

    ``_governed_action`` checks identity, then ``_pin`` runs caller-supplied
    metadata resolvers, then the decision client, audit sink and approval seam
    all run -- and only then does the body execute. Every one of those is a
    chance to move configuration the policy has already decided on.
    """
    tool = HttpTool(endpoint=GOOD)

    def moving_side_effect(target: Any) -> SideEffectClass:
        """Reconfigure the tool while describing it."""
        target.endpoint = EVIL
        return SideEffectClass.READ_ONLY

    [governed] = govern_tools(
        [tool], context=CONTEXT, client=Allowing(), side_effect=moving_side_effect
    )

    with pytest.raises(UnstableToolIdentityError):
        governed.invoke({"path": "x"})


def test_a_decision_client_cannot_move_declared_configuration_mid_call() -> None:
    """ZER18-AUD-001: the same window, one seam later."""
    tool = HttpTool(endpoint=GOOD)

    class Moving:
        """A decision client that reconfigures the tool it is deciding about."""

        def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            """Allow the call, having moved the endpoint out from under it."""
            tool.endpoint = EVIL
            return ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")

    [governed] = govern_tools([tool], context=CONTEXT, client=Moving(), side_effect=read_only)

    with pytest.raises(UnstableToolIdentityError):
        governed.invoke({"path": "x"})


def test_an_attribute_carrier_does_not_mask_a_differing_free_variable() -> None:
    """ZER18-AUD-002: precedence fingerprinted a value the body never reads.

    The attribute is identical in both tools; only the closed-over endpoint --
    the one the body actually uses -- differs. First-carrier-wins reported them
    as one tool.
    """

    def build(endpoint: str) -> Any:
        @identity_configuration("endpoint")
        def fetch(path: str) -> str:
            """Fetch a path."""
            return f"{endpoint}/{path}"

        fetch.endpoint = GOOD
        return fetch

    assert identity_of(build(GOOD)).fingerprint != identity_of(build(EVIL)).fingerprint


def test_a_second_body_slot_with_differing_configuration_changes_the_identity() -> None:
    """ZER18-AUD-002: the async body is a body, and its configuration counts.

    A tool whose sync half is good and async half is evil must not fingerprint as
    the all-good tool, or ``ainvoke`` runs an endpoint nothing authorized.
    """
    both_good = StructuredTool.from_function(
        func=make_fetcher(GOOD),
        coroutine=_async_fetcher(GOOD),
        name="fetch",
        description=DESCRIPTION,
    )
    mixed = StructuredTool.from_function(
        func=make_fetcher(GOOD),
        coroutine=_async_fetcher(EVIL),
        name="fetch",
        description=DESCRIPTION,
    )

    assert identity_of(both_good).fingerprint != identity_of(mixed).fingerprint


def test_a_declaration_that_is_not_an_exact_tuple_is_refused_without_iterating() -> None:
    """ZER18-AUD-003: reading a declaration must not run the delegate's code.

    ``list(declaration)`` executes ``__iter__``. Every other static read in this
    package refuses rather than calling into the tool, and a declaration is read
    on every call.
    """
    iterations: list[int] = []

    class HostileDeclaration:
        """A declaration that runs code when it is read."""

        def __iter__(self) -> Any:
            """Record that the delegate's code ran."""
            iterations.append(1)
            return iter(("endpoint",))

    class Sneaky(BaseTool):
        """Declares through a hostile iterable."""

        zeroth_identity_configuration: ClassVar[Any] = HostileDeclaration()

        name: str = "fetch"
        description: str = DESCRIPTION
        endpoint: str = GOOD

        def _run(self, path: str) -> str:
            """Return the path."""
            return path

    with pytest.raises(UnstableToolIdentityError):
        identity_of(Sneaky())
    assert iterations == []


def test_resolving_a_carrier_never_runs_a_stored_keys_equality() -> None:
    """ZER18-AUD-004: a dict lookup compares against stored keys.

    ``name in namespace`` gives a stored key's ``__eq__`` a chance to run, which
    is exactly what ``_mapping_material`` iterates ``items()`` to avoid. A hostile
    key can also answer for a field the tool does not really carry.
    """
    comparisons: list[int] = []

    class HostileKey(str):
        """A stored key that runs code when it is compared."""

        def __eq__(self, other: object) -> bool:
            """Record the comparison and answer as plain text would."""
            comparisons.append(1)
            return str.__eq__(self, other)

        def __hash__(self) -> int:
            """Hash as plain text, so the lookup reaches this key."""
            return str.__hash__(self)

    body = make_fetcher(GOOD)
    body.__dict__[HostileKey("endpoint")] = EVIL

    identity = identity_of(body)

    assert comparisons == []
    assert identity.fingerprint == identity_of(make_fetcher(GOOD)).fingerprint


def test_the_recorded_inventory_entry_preserves_the_declaration() -> None:
    """ZER18-AUD-005: R9 asks for the declaration in the inventory, not only the digest."""
    [governed] = govern_tools([HttpTool(endpoint=GOOD)])

    recorded = record_binding_inventory([governed.zeroth_binding])

    assert recorded.entries[0].identity_configuration == ("endpoint",)


def test_reading_a_class_declaration_never_runs_a_stored_keys_equality() -> None:
    """ZER18-AUD-004, second path: the class namespace is a namespace too.

    The declaration is looked up on the tool's class, and a class dictionary can
    hold a ``str`` subclass key just as an instance dictionary can. Keying into it
    hands that key's ``__eq__`` the same opportunity.

    A hostile key is skipped rather than refused, so the declaration it holds
    simply does not count: the tool governs with a code-only identity and the
    binding reports no declaration, which is what an operator needs to see.
    """
    comparisons: list[int] = []

    class HostileKey(str):
        """A stored class-dictionary key that runs code when it is compared."""

        def __eq__(self, other: object) -> bool:
            """Record the comparison and answer as plain text would."""
            comparisons.append(1)
            return str.__eq__(self, other)

        def __hash__(self) -> int:
            """Hash as plain text, so the lookup reaches this key."""
            return str.__hash__(self)

    hostile = type(
        "HostileDeclarationTool",
        (UndeclaredHttpTool,),
        {
            HostileKey(DECLARATION_ATTRIBUTE): ("endpoint",),
            "__annotations__": {DECLARATION_ATTRIBUTE: ClassVar[tuple]},
        },
    )
    comparisons.clear()

    [governed] = govern_tools([hostile(endpoint=GOOD)])

    assert comparisons == []
    assert governed.zeroth_binding.identity_configuration == ()


def test_resolving_a_carrier_never_runs_an_overridden_items() -> None:
    """ZER18-AUD-004, third path: ``items()`` is itself an overridable hook.

    Scanning entries instead of keying in only helps if the scan does not call
    the namespace's own method to get them. A namespace that is not exactly a
    ``dict`` is refused outright, so the hook is never reached either way -- the
    assertion that matters is that it never ran.
    """
    calls: list[int] = []

    class HostileNamespace(dict):
        """An instance dictionary that runs code when it is iterated."""

        def items(self) -> Any:
            """Record the call and answer as a plain dictionary would."""
            calls.append(1)
            return dict.items(self)

    body = make_fetcher(GOOD)
    hostile = HostileNamespace(body.__dict__)
    object.__setattr__(body, "__dict__", hostile)

    with pytest.raises(UnstableToolIdentityError):
        identity_of(body)
    assert calls == []


def test_a_tool_that_computes_its_own_namespace_is_refused() -> None:
    """ZER18-AUD-004, fourth path: ``__dict__`` is itself a class-defined descriptor.

    ``static_instance_dict`` reads ``__dict__`` through ``object.__getattribute__``,
    which finds whatever the *class* defines for that name. A class may define a
    ``property`` there, and then the instance-attribute carrier reads a value the
    tool computed after seeing that it was being asked.
    """
    reads: list[int] = []

    class ComputedNamespace:
        """A tool whose class answers ``__dict__`` with its own code."""

        name = "fetch"
        description = DESCRIPTION
        zeroth_identity_configuration = ("endpoint",)

        @property
        def __dict__(self) -> Any:
            """Record the read and answer with whatever suits."""
            reads.append(1)
            return {"endpoint": GOOD}

        def __call__(self, path: str) -> str:
            """Fetch a path."""
            return path

    with pytest.raises(UnstableToolIdentityError):
        identity_of(ComputedNamespace())
    assert reads == []


def test_every_ordinary_declaring_shape_still_governs() -> None:
    """The refusals above must cost no tool a person would actually write.

    Every probe in this file otherwise asserts a refusal or a zero hook count, so
    an over-broad namespace gate would pass all of them while breaking ordinary
    tools. This is the positive control for that.
    """

    class Configured(BaseTool):
        """A hand-written declaring tool."""

        zeroth_identity_configuration: ClassVar[tuple[str, ...]] = ("endpoint",)

        name: str = "fetch"
        description: str = DESCRIPTION
        endpoint: str = GOOD

        def _run(self, path: str) -> str:
            """Fetch a path."""
            return f"{self.endpoint}/{path}"

    class CallableTool:
        """A declaring callable object, configured per instance.

        The declared field has to be on the *instance*, which is what "instance
        configuration" means: a class attribute is shared by every instance and
        so distinguishes none of them. An author who declares a field the
        instance does not carry is refused, which
        ``test_a_declared_name_that_names_nothing_refuses_the_tool`` pins.
        """

        name = "fetch"
        description = DESCRIPTION
        zeroth_identity_configuration = ("endpoint",)

        def __init__(self, endpoint: str) -> None:
            """Configure this instance."""
            self.endpoint = endpoint

        def __call__(self, path: str) -> str:
            """Fetch a path."""
            return f"{self.endpoint}/{path}"

    for target in (
        Configured(),
        make_fetcher(GOOD),
        structured(make_fetcher(GOOD)),
        CallableTool(GOOD),
    ):
        [governed] = govern_tools([target])
        assert governed.zeroth_binding.identity_configuration == ("endpoint",)
