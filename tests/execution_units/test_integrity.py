from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from zeroth.audit import AuditRepository
from zeroth.execution_units import (
    AdmissionController,
    CommandArtifactSource,
    ExecutableUnitBinding,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    WrappedCommandUnitManifest,
    compute_manifest_digest,
)
from zeroth.graph import ExecutableUnitNode, ExecutableUnitNodeData, Graph
from zeroth.orchestrator import RuntimeOrchestrator
from zeroth.runs import RunRepository, RunStatus


class DemoInput(BaseModel):
    value: int


class DemoOutput(BaseModel):
    value: int


def _manifest(script: Path, *, unit_id: str = "digest-unit") -> WrappedCommandUnitManifest:
    return WrappedCommandUnitManifest(
        unit_id=unit_id,
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )


def test_manifest_digest_is_stable_and_deterministic(tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    manifest = _manifest(script)

    assert compute_manifest_digest(manifest) == compute_manifest_digest(manifest.model_copy())


def test_admission_controller_admits_trusted_manifest(tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    manifest = _manifest(script)
    controller = AdmissionController(
        allowed_runtimes={"command"},
        allowed_commands={sys.executable},
    )
    controller.register_trusted_digest(manifest.unit_id, compute_manifest_digest(manifest))

    result = controller.admit(manifest)

    assert result.admitted is True
    assert result.reason == "trusted_digest"


def test_admission_controller_rejects_modified_manifest(tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    manifest = _manifest(script)
    controller = AdmissionController()
    controller.register_trusted_digest(manifest.unit_id, compute_manifest_digest(manifest))
    modified = manifest.model_copy(
        update={"run_config": RunConfig(command=["python", str(script)])}
    )

    result = controller.admit(modified)

    assert result.admitted is False
    assert "digest" in result.reason


def test_admission_controller_rejects_untrusted_runtime_or_command(tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    manifest = _manifest(script)
    controller = AdmissionController(allowed_runtimes={"python"}, allowed_commands={"python"})

    runtime_result = controller.admit(manifest)

    assert runtime_result.admitted is False
    assert "runtime" in runtime_result.reason or "command" in runtime_result.reason


@pytest.mark.asyncio
async def test_executable_unit_admission_denials_are_recorded_in_audit(
    sqlite_db,
    tmp_path: Path,
) -> None:
    script = tmp_path / "tool.py"
    script.write_text("print('ok')", encoding="utf-8")
    manifest = _manifest(script, unit_id="audit-unit")
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://audit-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )
    runner = ExecutableUnitRunner(
        registry,
        admission_controller=AdmissionController(allowed_runtimes={"python"}),
    )
    graph = Graph(
        graph_id="graph-integrity",
        name="integrity",
        entry_step="eu",
        nodes=[
            ExecutableUnitNode(
                node_id="eu",
                graph_version_ref="graph-integrity:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://audit-unit",
                    execution_mode="wrapped_command",
                ),
            )
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={},
        executable_unit_runner=runner,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.FAILED
    audits = AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert len(audits) == 1
    assert audits[0].status == "rejected"
    assert audits[0].execution_metadata["admission"]["admitted"] is False
