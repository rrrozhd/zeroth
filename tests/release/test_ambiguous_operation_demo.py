from __future__ import annotations

from zeroth.platform.dispatch.operations import OperationState
from zeroth.platform.signing import EnvHmacSigner

from release.live_evaluation.ambiguous_operation_demo import (
    seed_ambiguous_operation_demo,
)
from scripts.zeroth_dev_instance import _parser


async def test_seed_ambiguous_operation_demo_writes_signed_run_evidence(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="fixture", keys={"fixture": b"fixture-signing-key"})

    fixture = await seed_ambiguous_operation_demo(
        sqlite_db,
        tenant_id="default",
        workspace_id=None,
        deployment_ref="deployment-d012",
        graph_version_ref="graph-d012@1",
        signer=signer,
        fixture_id="test-fixture",
    )

    assert fixture.run.run_id == "ambiguous-demo-run-test-fixture"
    assert fixture.operation["state"] == OperationState.AMBIGUOUS
    assert fixture.audit.record_signature is not None
    assert fixture.audit.tool_calls[0].operation_key == fixture.operation_key
    assert fixture.audit.tool_calls[0].operation_state == OperationState.AMBIGUOUS
    assert fixture.audit.tool_calls[0].operation_reconciliation_required is True
    assert fixture.audit.tool_calls[0].operation_replay_suppressed is False


async def test_seed_ambiguous_operation_demo_is_idempotent(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="fixture", keys={"fixture": b"fixture-signing-key"})
    kwargs = {
        "tenant_id": "default",
        "workspace_id": None,
        "deployment_ref": "deployment-d012",
        "graph_version_ref": "graph-d012@1",
        "signer": signer,
        "fixture_id": "repeatable",
    }

    first = await seed_ambiguous_operation_demo(sqlite_db, **kwargs)
    second = await seed_ambiguous_operation_demo(sqlite_db, **kwargs)

    assert second.run == first.run
    assert second.audit.audit_id == first.audit.audit_id
    assert second.operation["state"] == OperationState.AMBIGUOUS


def test_dev_instance_cli_exposes_ambiguous_operation_demo_seed() -> None:
    parsed = _parser().parse_args(["seed-ambiguous-operation-demo"])

    assert parsed.command == "seed-ambiguous-operation-demo"
