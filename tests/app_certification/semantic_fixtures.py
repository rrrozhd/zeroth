from __future__ import annotations

from pydantic import BaseModel

from apps.vendor_dd.graphs import build_main_graph
from zeroth.governance.policy import PolicyGuard


class FixtureContract(BaseModel):
    value: str


VALID_CONTRACTS = {"contract://fixture/value": FixtureContract}
INVALID_CONTRACTS = {"contract://fixture/value": object}


def invalid_graph():
    graph = build_main_graph()
    graph.nodes[0].input_contract_ref = "contract://missing/input"
    return graph


def empty_policy_guard():
    return PolicyGuard()


def invalid_auth_config():
    return object()
