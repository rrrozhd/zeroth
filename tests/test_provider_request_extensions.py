"""Tests for extended data models: ProviderRequest, ModelParams, ToolAttachmentManifest, AgentConfig, AgentNodeData."""

from __future__ import annotations

from pydantic import BaseModel

from zeroth.core.agent_runtime.models import AgentConfig
from zeroth.core.agent_runtime.provider import ModelParams, ProviderRequest
from zeroth.core.agent_runtime.tools import ToolAttachmentManifest
from zeroth.core.graph.models import AgentNodeData

# ---------------------------------------------------------------------------
# ProviderRequest backward compatibility
# ---------------------------------------------------------------------------


class TestProviderRequestBackwardCompat:
    """ProviderRequest with no new fields still works."""

    def test_no_new_fields(self) -> None:
        req = ProviderRequest(model_name="gpt-4o")
        assert req.model_name == "gpt-4o"
        assert req.tools is None
        assert req.tool_choice is None
        assert req.response_format is None
        assert req.output_model is None
        assert req.model_params is None


# ---------------------------------------------------------------------------
# ProviderRequest new fields
# ---------------------------------------------------------------------------


class TestProviderRequestNewFields:
    def test_tools_accepted(self) -> None:
        req = ProviderRequest(
            model_name="x",
            messages=[],
            metadata={},
            tools=[{"type": "function", "function": {"name": "f"}}],
        )
        assert req.tools == [{"type": "function", "function": {"name": "f"}}]

    def test_tool_choice_accepted(self) -> None:
        req = ProviderRequest(model_name="x", tool_choice="auto")
        assert req.tool_choice == "auto"

    def test_response_format_accepted(self) -> None:
        fmt = {"type": "json_schema", "json_schema": {"name": "T", "schema": {}}}
        req = ProviderRequest(model_name="x", response_format=fmt)
        assert req.response_format == fmt

    def test_model_params_accepted(self) -> None:
        req = ProviderRequest(
            model_name="x",
            model_params=ModelParams(temperature=0.7),
        )
        assert req.model_params is not None
        assert req.model_params.temperature == 0.7

    def test_output_model_accepted(self) -> None:
        class MyOutput(BaseModel):
            value: str

        req = ProviderRequest(model_name="x", output_model=MyOutput)
        assert req.output_model is MyOutput


# ---------------------------------------------------------------------------
# ModelParams
# ---------------------------------------------------------------------------


class TestModelParams:
    def test_valid_instance(self) -> None:
        mp = ModelParams(temperature=0.7)
        assert mp.temperature == 0.7

    def test_all_none_defaults(self) -> None:
        mp = ModelParams()
        assert mp.temperature is None
        assert mp.top_p is None
        assert mp.max_tokens is None
        assert mp.stop is None
        assert mp.seed is None


# ---------------------------------------------------------------------------
# ToolAttachmentManifest extensions
# ---------------------------------------------------------------------------


class TestToolAttachmentManifestExtensions:
    def test_description_and_parameters_schema_accepted(self) -> None:
        m = ToolAttachmentManifest(
            alias="f",
            executable_unit_ref="r",
            description="desc",
            parameters_schema={"type": "object"},
        )
        assert m.description == "desc"
        assert m.parameters_schema == {"type": "object"}

    def test_to_openai_tool(self) -> None:
        m = ToolAttachmentManifest(
            alias="f",
            executable_unit_ref="r",
            description="d",
            parameters_schema={"type": "object"},
        )
        result = m.to_openai_tool()
        assert result == {
            "type": "function",
            "function": {
                "name": "f",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }

    def test_backward_compat_no_description_no_schema(self) -> None:
        m = ToolAttachmentManifest(alias="f", executable_unit_ref="r")
        assert m.description == ""
        assert m.parameters_schema is None


# ---------------------------------------------------------------------------
# AgentConfig extensions
# ---------------------------------------------------------------------------


class _DummyInput(BaseModel):
    x: int = 0


class _DummyOutput(BaseModel):
    y: int = 0


class TestAgentConfigExtensions:
    def test_model_params_accepted(self) -> None:
        cfg = AgentConfig(
            name="a",
            instruction="do stuff",
            model_name="gpt-4o",
            input_model=_DummyInput,
            output_model=_DummyOutput,
            model_params=ModelParams(temperature=0.5),
        )
        assert cfg.model_params is not None
        assert cfg.model_params.temperature == 0.5

    def test_backward_compat_no_model_params(self) -> None:
        cfg = AgentConfig(
            name="a",
            instruction="do stuff",
            model_name="gpt-4o",
            input_model=_DummyInput,
            output_model=_DummyOutput,
        )
        assert cfg.model_params is None


# ---------------------------------------------------------------------------
# AgentNodeData extensions
# ---------------------------------------------------------------------------


class TestAgentNodeDataExtensions:
    def test_model_params_accepted(self) -> None:
        nd = AgentNodeData(
            instruction="go",
            model_provider="openai/gpt-4o",
            model_params={"temperature": 0.5},
        )
        assert nd.model_params == {"temperature": 0.5}

    def test_mcp_servers_accepted(self) -> None:
        nd = AgentNodeData(
            instruction="go",
            model_provider="openai/gpt-4o",
            mcp_servers=[{"name": "s", "command": "python", "args": []}],
        )
        assert nd.mcp_servers == [{"name": "s", "command": "python", "args": []}]
