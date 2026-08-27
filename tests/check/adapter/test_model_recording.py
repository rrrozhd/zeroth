from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from zeroth.check.adapter.langgraph_recording import LangGraphRecordingHandler


def test_records_complete_content_free_model_usage() -> None:
    handler = LangGraphRecordingHandler(registrations={}, case_id="case", scenario_run_id="run")
    run_id = uuid4()
    handler.on_chat_model_start(
        {"id": ["langchain", "chat_models", "openai", "ChatOpenAI"]},
        [[SimpleNamespace(type="human", content="private prompt", tool_calls=[])]],
        run_id=run_id,
        metadata={"ls_provider": "openai", "ls_model_name": "gpt-test"},
    )
    message = SimpleNamespace(
        type="ai",
        content="private answer",
        tool_calls=[],
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_token_details": {"cache_read": 0},
            "output_token_details": {"reasoning": 0},
        },
        response_metadata={},
    )
    handler.on_llm_end(
        SimpleNamespace(generations=[[SimpleNamespace(message=message)]], llm_output={}),
        run_id=run_id,
    )
    observation = handler.model_calls[0]
    assert observation.provider == "openai"
    assert observation.model == "gpt-test"
    assert observation.total_tokens == 5
    assert "private prompt" not in repr(observation)
    assert "private answer" not in repr(observation)


def test_incomplete_usage_remains_an_explicit_model_call() -> None:
    handler = LangGraphRecordingHandler(registrations={}, case_id="case", scenario_run_id="run")
    run_id = uuid4()
    handler.on_llm_start({}, ["prompt"], run_id=run_id)
    handler.on_llm_end(SimpleNamespace(generations=[], llm_output={}), run_id=run_id)
    observation = handler.model_calls[0]
    assert observation.provider is None
    assert observation.input_tokens is None
    assert observation.input_details is None
