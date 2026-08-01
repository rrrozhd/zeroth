"""The adapter version constant and the mixed-version agreement rule.

``ADAPTER_VERSION`` identifies the governed-LangGraph adapter's own wire
contract, independent of ``pyproject.toml``'s repo-wide release version. The
two move on different clocks: a release can ship without touching a single
byte of the attestation/registration wire format, and the wire format must be
free to change (rarely) without forcing a release. Bumping
``ADAPTER_VERSION`` on every release would make it useless for detecting an
actual contract drift between what a running adapter emits and what the
gateway that registered it expects.

Graph-version mismatch already clamps a run to ``ADMISSION`` in
``zeroth.core.langgraph_gateway.capabilities``. Adapter-version mismatch has
no independent handling before this module: ``classify_version_agreement``
gives the two axes -- graph and adapter -- a shared vocabulary, and
``permits_full_enforcement`` gives callers a single, deliberately narrow gate
so a later subtask cannot accidentally read "unknown" as "fine".

The rule, exactly:

* Any of the four inputs missing, ``None``, or the empty string classifies as
  ``UNKNOWN``. An absent version is not evidence of agreement -- it is the
  *absence* of the evidence agreement would require, so this fails closed
  rather than falling through to a mismatch or (worse) a match.
* Both pairs matching classifies as ``MATCH``.
* Otherwise the specific axis (or both axes) that disagree names the result:
  ``ADAPTER_MISMATCH``, ``GRAPH_MISMATCH``, or ``BOTH_MISMATCH``.

Comparison is exact string equality on both axes. There is no semver range or
compatibility logic here on purpose: a governance claim ("this run is fully
enforced") must not depend on someone's interpretation of what counts as a
compatible version range. If the wire contract changes in a way that is truly
compatible, that is expressed by *not* bumping ``ADAPTER_VERSION`` -- not by
teaching this module to reason about ranges.
"""

from __future__ import annotations

from enum import StrEnum

ADAPTER_VERSION = "1.0"
"""Version of the governed-LangGraph adapter's attestation/registration wire
contract.

Bump this when the *shape or meaning* of what the adapter attests or
registers changes -- new required field, changed semantics of an existing
field, changed encoding -- not on every release of the adapter or the repo.
It is intentionally decoupled from ``pyproject.toml``'s version so the wire
contract can be versioned on its own schedule.
"""


class VersionAgreement(StrEnum):
    """Result of comparing expected vs. actual graph and adapter versions."""

    MATCH = "match"
    ADAPTER_MISMATCH = "adapter_mismatch"
    GRAPH_MISMATCH = "graph_mismatch"
    BOTH_MISMATCH = "both_mismatch"
    UNKNOWN = "unknown"


def classify_version_agreement(
    *,
    expected_graph_version: str | None,
    actual_graph_version: str | None,
    expected_adapter_version: str | None,
    actual_adapter_version: str | None,
) -> VersionAgreement:
    """Classify how expected and actual graph/adapter versions agree.

    See the module docstring for the full rule. In short: any missing or
    empty side on either pair fails closed to ``UNKNOWN`` before either pair
    is compared; otherwise the two pairs are compared independently by exact
    string equality and the result names which axis (if any) disagrees.

    Args:
        expected_graph_version: Graph version the gateway expects for this
            deployment, or ``None``/empty if unavailable.
        actual_graph_version: Graph version attested by the client, or
            ``None``/empty if unavailable.
        expected_adapter_version: Adapter version the gateway expects
            (typically ``ADAPTER_VERSION``), or ``None``/empty if
            unavailable.
        actual_adapter_version: Adapter version attested by the client, or
            ``None``/empty if unavailable.

    Returns:
        The ``VersionAgreement`` member describing the comparison outcome.
    """
    versions = (
        expected_graph_version,
        actual_graph_version,
        expected_adapter_version,
        actual_adapter_version,
    )
    if any(version is None or version == "" for version in versions):
        return VersionAgreement.UNKNOWN

    graph_matches = expected_graph_version == actual_graph_version
    adapter_matches = expected_adapter_version == actual_adapter_version

    if graph_matches and adapter_matches:
        return VersionAgreement.MATCH
    if not graph_matches and not adapter_matches:
        return VersionAgreement.BOTH_MISMATCH
    if adapter_matches:
        return VersionAgreement.GRAPH_MISMATCH
    return VersionAgreement.ADAPTER_MISMATCH


def permits_full_enforcement(agreement: VersionAgreement) -> bool:
    """Return whether ``agreement`` may support a full-enforcement claim.

    Only ``VersionAgreement.MATCH`` permits it. Every other member --
    including ``UNKNOWN`` -- forbids it: an absent or unverifiable version is
    not a reason to grant enforcement, and a caller must not be able to read
    "we don't know" as "it's fine". Implemented as an explicit equality check
    against ``MATCH`` rather than an allowlist/denylist of other members, so a
    future member added to ``VersionAgreement`` without updating this
    function still forbids enforcement by default instead of silently
    permitting it.

    Args:
        agreement: The classification to gate on.

    Returns:
        True only for ``VersionAgreement.MATCH``; False for everything else.
    """
    return agreement is VersionAgreement.MATCH
