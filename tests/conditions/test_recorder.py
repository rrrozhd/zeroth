from __future__ import annotations

from zeroth.core.conditions import ConditionResultRecorder
from zeroth.core.runs.models import Run, RunConditionResult


def test_condition_result_recorder_appends_to_run_state() -> None:
    run = Run(graph_version_ref="graph:v1", deployment_ref="deployment:v1")
    first_result = RunConditionResult(
        condition_id="edge-ab",
        selected_edge_id="edge-ab",
        matched=True,
        details={"source_node_id": "node-a"},
    )
    second_result = RunConditionResult(
        condition_id="edge-ac",
        selected_edge_id=None,
        matched=False,
        details={"source_node_id": "node-a"},
    )

    recorder = ConditionResultRecorder()
    recorded = recorder.record_many(run, [first_result, second_result])

    assert recorded is run
    assert recorded.condition_results == [first_result, second_result]
