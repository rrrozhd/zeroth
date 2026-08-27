"""Owned loopback server entrypoint for the live-evaluation scenario controller."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .action_sink import EvaluationActionSink
from .campaign_execution import CampaignExecutionSettings, build_campaign_execution
from .config import CampaignConfig
from .evidence import EvidenceStore
from .fault_control import EvaluationFaultState
from .receipt_restart_barrier import ReceiptRestartBarrierStore
from .scenario_controller import (
    LoopbackDeployment,
    LoopbackHttpScenarioRuntimeGateway,
    create_scenario_controller_app,
)


class OwnedScenarioControllerServer:
    """Run an ASGI controller in-process so it shares deployment ownership."""

    def __init__(self, *, app: object, base_url: str) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname
            not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            or not isinstance(parsed.port, int)
        ):
            raise ValueError("owned scenario controller requires a loopback origin")
        import uvicorn

        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=parsed.hostname,
                port=parsed.port,
                log_level="warning",
                access_log=False,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            name="evaluation-scenario-controller",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.05)
        raise RuntimeError("owned scenario controller failed to start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("owned scenario controller failed to stop")


def _required_secret(environment: Mapping[str, str], name: str, label: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{label} environment variable is required")
    return value


def _deployment_urls(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        reference, separator, url = value.partition("=")
        if not separator or not reference or not url or reference in result:
            raise ValueError("deployment URLs must be unique REF=URL pairs")
        result[reference] = url
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m release.live_evaluation.scenario_controller_server"
    )
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--evidence-bundle", type=Path, required=True)
    parser.add_argument("--deployment-url", action="append", default=[], metavar="REF=URL")
    parser.add_argument("--workspace-id")
    parser.add_argument("--api-key-env", default="ZEROTH_EVALUATION_API_KEY")
    parser.add_argument(
        "--controller-key-env",
        default="ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY",
    )
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    return parser


def compose_app(args: argparse.Namespace, environment: Mapping[str, str]):
    campaign = CampaignConfig.model_validate(
        json.loads(args.campaign_config.read_text(encoding="utf-8"))
    )
    api_key = _required_secret(environment, args.api_key_env, "Zeroth API key")
    controller_key = _required_secret(
        environment, args.controller_key_env, "scenario controller key"
    )
    urls = _deployment_urls(args.deployment_url)
    execution = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id=campaign.campaign_id,
            tenant_id=campaign.tenant_id,
            model=campaign.model,
            embedding_model=campaign.embedding_model,
            chroma_connector_ref="chroma",
            workspace_id=args.workspace_id,
        )
    )
    refs = execution.deployments
    expected = {
        refs.workflow1,
        refs.workflow2_child,
        refs.workflow2_parent,
        refs.workflow3,
    }
    if set(urls) != expected:
        raise ValueError("deployment URL map must exactly cover the campaign topology")
    store = EvidenceStore(args.evidence_bundle)
    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id=campaign.campaign_id,
        deployments={
            reference: LoopbackDeployment(base_url=url, deployment_ref=reference)
            for reference, url in urls.items()
        },
        client=httpx.Client(),
        headers={"X-API-Key": api_key, "X-Tenant-ID": campaign.tenant_id},
        # Standalone mode never claims ownership of campaign-started processes.
        # Restart scenarios require the campaign's in-process owned server.
        supervisor=None,
        receipt_barriers=ReceiptRestartBarrierStore(
            campaign.artifact_root / "receipt-restart-barriers.sqlite3"
        ),
    )
    return create_scenario_controller_app(
        campaign_id=campaign.campaign_id,
        artifact_root=campaign.artifact_root,
        evidence_store=store,
        fault_state=EvaluationFaultState(campaign.artifact_root / "fault-control.sqlite3"),
        action_sink=EvaluationActionSink(campaign.action_sink_root),
        controller_key=controller_key,
        runtime_gateway=gateway,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise ValueError("scenario-controller port is invalid")
    app = compose_app(args, os.environ)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution boundary
    raise SystemExit(main())
