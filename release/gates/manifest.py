"""Load and structurally validate the release-gate manifest.

The manifest is data rather than a Python dict so that later ZER-28 work can
add a gate without editing the validator. That only holds if the manifest is
itself checked: a manifest that silently loses a gate would turn a fail-closed
system into an open one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gates.identity import FACETS

SCHEMA_VERSION = 1
PHASES = ("candidate", "final")
TRIGGERS = ("pull-request", "nightly", "release-candidate", "manual")
PRODUCERS = ("ci", "manual")

#: Every gate family the ticket requires. A manifest that does not name all of
#: them is rejected, so a gate cannot be dropped by editing data alone.
REQUIRED_GATES = frozenset(
    {
        "source",
        "package",
        "langgraph",
        "security-regression",
        "untrusted-code",
        "deployment-smoke",
        "remote-acceptance",
        "promotion",
    }
)

#: Every evidence kind the acceptance criteria enumerate. Each must be declared
#: either applicable, or not applicable *with a stated reason*.
REQUIRED_KINDS = frozenset(
    {
        "junit",
        "compatibility",
        "benchmark",
        "security",
        "deployment",
        "ui",
        "sbom",
        "provenance",
        "manual-signoff",
    }
)

_GATE_KEYS = frozenset(
    {
        "id",
        "order",
        "phase",
        "title",
        "description",
        "binds",
        "record",
        "requires",
        "kinds",
        "triggers",
    }
)

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "release-gates.json"


class ManifestError(ValueError):
    """The manifest is malformed, so nothing downstream may be trusted."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _check_gate(gate: Any, kinds: dict[str, Any], seen: set[str]) -> None:
    _require(isinstance(gate, dict), "each gate must be an object")
    _require(set(gate) == _GATE_KEYS, f"gate {gate.get('id')!r} has unexpected keys")
    identifier = gate["id"]
    _require(identifier not in seen, f"duplicate gate {identifier!r}")
    seen.add(identifier)
    _require(gate["phase"] in PHASES, f"gate {identifier!r} has an unknown phase")
    _require(
        isinstance(gate["order"], int) and not isinstance(gate["order"], bool),
        f"gate {identifier!r} needs an integer order",
    )
    _require(bool(gate["title"]) and bool(gate["description"]), f"gate {identifier!r} needs prose")
    _require(
        isinstance(gate["binds"], list)
        and gate["binds"]
        and all(facet in FACETS for facet in gate["binds"]),
        f"gate {identifier!r} must bind known identity facets",
    )
    _require(
        isinstance(gate["requires"], list) and bool(gate["requires"]),
        f"gate {identifier!r} must require at least one result",
    )
    _require(
        isinstance(gate["kinds"], list)
        and bool(gate["kinds"])
        and all(kind in kinds for kind in gate["kinds"]),
        f"gate {identifier!r} names an undeclared evidence kind",
    )
    _require(
        isinstance(gate["triggers"], list)
        and bool(gate["triggers"])
        and all(trigger in TRIGGERS for trigger in gate["triggers"]),
        f"gate {identifier!r} names an unknown trigger",
    )
    _require(
        isinstance(gate["record"], str) and gate["record"].endswith(".json"),
        f"gate {identifier!r} needs a JSON record path",
    )


def _check_kinds(kinds: Any) -> None:
    _require(isinstance(kinds, dict), "evidence_kinds must be an object")
    _require(
        set(kinds) == REQUIRED_KINDS,
        "evidence_kinds must declare exactly the acceptance-criteria kinds",
    )
    for name, kind in kinds.items():
        _require(isinstance(kind, dict), f"evidence kind {name!r} must be an object")
        _require(
            isinstance(kind.get("applicable"), bool),
            f"evidence kind {name!r} must declare applicability",
        )
        _require(bool(kind.get("description")), f"evidence kind {name!r} needs a description")
        if kind["applicable"]:
            _require(
                kind.get("producer") in PRODUCERS,
                f"applicable evidence kind {name!r} needs a known producer",
            )
        else:
            # "as applicable" must never mean "quietly dropped".
            _require(
                bool(kind.get("reason")),
                f"evidence kind {name!r} is not applicable and must state why",
            )


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Return the parsed manifest, rejecting any structurally invalid one."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"manifest unreadable: {error}") from error
    _require(isinstance(manifest, dict), "manifest must be an object")
    _require(
        set(manifest) == {"schema_version", "gates", "evidence_kinds"},
        "manifest has unexpected top-level keys",
    )
    _require(manifest["schema_version"] == SCHEMA_VERSION, "unsupported manifest schema_version")
    _check_kinds(manifest["evidence_kinds"])
    gates = manifest["gates"]
    _require(isinstance(gates, list) and bool(gates), "manifest must list gates")
    seen: set[str] = set()
    for gate in gates:
        _check_gate(gate, manifest["evidence_kinds"], seen)
    # A superset is allowed: ZER-28's later tickets add gates, and the docs
    # promise that adding one is a manifest edit. What may never happen is a
    # required gate going missing, which would quietly widen promotion.
    missing = REQUIRED_GATES - seen
    _require(not missing, f"manifest is missing required gates: {', '.join(sorted(missing))}")
    orders = [gate["order"] for gate in gates]
    _require(orders == sorted(orders), "gates must be listed in their evaluation order")
    _require(len(set(orders)) == len(orders), "gate order must be unambiguous")
    # A candidate gate may never be ordered after a final one: promotion has to
    # be able to depend on everything that precedes it.
    phases = [gate["phase"] for gate in sorted(gates, key=lambda item: item["order"])]
    _require(
        phases == sorted(phases, key=PHASES.index),
        "every candidate gate must be ordered before the final gates",
    )
    return manifest


def gates_for_phase(manifest: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    """Return the gates a given phase must validate, in evaluation order.

    ``candidate`` covers everything that precedes promotion. ``final`` covers
    every gate, because promotion may only happen once all of them hold.
    """
    _require(phase in PHASES, f"unknown phase {phase!r}")
    gates = sorted(manifest["gates"], key=lambda item: item["order"])
    if phase == "final":
        return gates
    return [gate for gate in gates if gate["phase"] == "candidate"]


def select_gates(
    manifest: dict[str, Any], *, phase: str = "final", trigger: str | None = None
) -> list[dict[str, Any]]:
    """Return the gates a run must validate, in evaluation order.

    ``trigger`` narrows the phase to the gates that actually run at that
    trigger, so a nightly is not blocked by evidence only a release candidate
    produces. It never widens: a gate outside the phase stays outside it.
    """
    gates = gates_for_phase(manifest, phase)
    if trigger is None:
        return gates
    _require(trigger in TRIGGERS, f"unknown trigger {trigger!r}")
    return [gate for gate in gates if trigger in gate["triggers"]]


def applicable_kinds(manifest: dict[str, Any], producer: str) -> list[str]:
    """Return the applicable evidence kinds a given producer is responsible for."""
    return sorted(
        name
        for name, kind in manifest["evidence_kinds"].items()
        if kind["applicable"] and kind.get("producer") == producer
    )
