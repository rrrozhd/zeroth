"""Provider-free unified readiness gate for the remaining live checkpoints.

The CLI validates only non-secret configuration and persisted-path inventory.
It never opens campaign databases, performs HTTP, resolves service/provider
credentials, or invokes a paid adapter.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .batch_provider_economics import (
    BatchProviderEconomicsHarness,
    LiveBatchGate,
    ReadinessAttestation,
)
from .campaign_http import provider_acknowledgement
from .config import CampaignConfig
from .rightsizing_service_adapter import ExperimentRequest
from .template_live_rendered_execution import LiveTemplateConfig, LiveTemplateFixture

ARM_ENVIRONMENT_VARIABLE = "ZEROTH_ARM_FULL_LIVE_PROVIDER_GATE"
PLANNED_ORDER = (
    "batching.provider-economics",
    "templates.live-rendered-execution",
    "rightsizing.measured-experiment",
    "rightsizing.cost-reconciliation",
)
_WIRING_KEYS = {
    "schema_version",
    "service_base_url",
    "service_database",
    "econ_database",
    "action_sink_database",
    "provider_window",
    "batch_items",
    "template",
    "rightsizing",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReadinessBlocked(ValueError):  # noqa: N818
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProviderFreeWiring:
    service_base_url: str
    service_database: Path
    econ_database: Path
    action_sink_database: Path
    provider_window: Path
    batch_items: tuple[Mapping[str, object], ...]
    template_config: LiveTemplateConfig
    template_fixture: LiveTemplateFixture
    rightsizing_request: ExperimentRequest
    rightsizing_cases_sha256: str


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    campaign_id: str
    tenant_id: str
    configuration_ready: bool
    armed: bool
    execution_ready: bool
    blockers: tuple[str, ...]
    planned_order: tuple[str, ...] = PLANNED_ORDER
    provider_calls_performed: int = 0

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["blockers"] = list(self.blockers)
        value["planned_order"] = list(self.planned_order)
        return value


def _object(path: Path, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ReadinessBlocked(code) from None
    if not isinstance(value, dict):
        raise ReadinessBlocked(code)
    return value


def _loopback_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ReadinessBlocked("service_origin_invalid")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        loopback = host == "localhost" or (
            host is not None and ipaddress.ip_address(host).is_loopback
        )
    except ValueError:
        raise ReadinessBlocked("service_origin_invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ReadinessBlocked("service_origin_invalid")
    return value.rstrip("/")


def _required_file(value: object) -> Path:
    if not isinstance(value, str):
        raise ReadinessBlocked("persisted_plane_inventory_incomplete")
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise ReadinessBlocked("persisted_plane_inventory_incomplete")
    return path


def _parse_wiring(raw: Mapping[str, object]) -> ProviderFreeWiring:
    if set(raw) != _WIRING_KEYS or raw.get("schema_version") != 1:
        raise ReadinessBlocked("wiring_configuration_invalid")
    items = raw.get("batch_items")
    if (
        not isinstance(items, list)
        or len(items) != 8
        or any(
            not isinstance(item, dict) or item.get("index") != index
            for index, item in enumerate(items)
        )
    ):
        raise ReadinessBlocked("batch_wiring_invalid")
    template = raw.get("template")
    rightsizing = raw.get("rightsizing")
    if not isinstance(template, dict) or not isinstance(rightsizing, dict):
        raise ReadinessBlocked("wiring_configuration_invalid")
    template_config_keys = {
        "fixture_id",
        "tenant_id",
        "template_name",
        "deployment_ref",
    }
    fixture_keys = {
        "fixture_id",
        "template_name",
        "template_version",
        "workflow_id",
        "graph_version_ref",
        "deployment_ref",
        "deployment_version",
        "provider_calls_performed",
    }
    if set(template) != template_config_keys | (fixture_keys - template_config_keys):
        raise ReadinessBlocked("template_wiring_invalid")
    try:
        template_config = LiveTemplateConfig(**{key: template[key] for key in template_config_keys})
        template_fixture = LiveTemplateFixture(**{key: template[key] for key in fixture_keys})
    except (TypeError, ValueError):
        raise ReadinessBlocked("template_wiring_invalid") from None
    cases_sha256 = rightsizing.get("cases_sha256")
    request_fields = set(rightsizing) - {"cases_sha256"}
    if not isinstance(cases_sha256, str) or not _SHA256.fullmatch(cases_sha256):
        raise ReadinessBlocked("rightsizing_wiring_invalid")
    try:
        request = ExperimentRequest(**{key: rightsizing[key] for key in request_fields})
    except (TypeError, ValueError):
        raise ReadinessBlocked("rightsizing_wiring_invalid") from None
    return ProviderFreeWiring(
        service_base_url=_loopback_origin(raw.get("service_base_url")),
        service_database=_required_file(raw.get("service_database")),
        econ_database=_required_file(raw.get("econ_database")),
        action_sink_database=_required_file(raw.get("action_sink_database")),
        provider_window=_required_file(raw.get("provider_window")),
        batch_items=tuple(items),
        template_config=template_config,
        template_fixture=template_fixture,
        rightsizing_request=request,
        rightsizing_cases_sha256=cases_sha256,
    )


def readiness(
    *,
    campaign: CampaignConfig,
    attestation: ReadinessAttestation,
    wiring: ProviderFreeWiring,
    arm_live_provider: bool,
    environment: Mapping[str, str],
) -> ReadinessResult:
    if campaign.campaign_id != campaign.tenant_id:
        # The existing template checkpoint deliberately uses its tenant as the
        # strict campaign tag. Until that product contract is separated, a
        # unified run with distinct identities would misattribute spend.
        raise ReadinessBlocked("template_campaign_tenant_identity_mismatch")
    if wiring.template_config.tenant_id != campaign.tenant_id:
        raise ReadinessBlocked("template_campaign_tenant_identity_mismatch")
    fixture = wiring.template_fixture
    if (
        fixture.fixture_id != wiring.template_config.fixture_id
        or fixture.template_name != wiring.template_config.template_name
        or fixture.deployment_ref != wiring.template_config.deployment_ref
        or fixture.template_version != 1
        or fixture.deployment_version != 1
        or not fixture.graph_version_ref.endswith("@1")
        or fixture.provider_calls_performed != 0
    ):
        raise ReadinessBlocked("template_wiring_invalid")
    if (
        campaign.provider_secret_ref != "llm.openai"
        or wiring.rightsizing_request.incumbent != campaign.model
        or wiring.rightsizing_request.judge_model not in {None, campaign.model}
    ):
        raise ReadinessBlocked("provider_model_wiring_mismatch")
    try:
        BatchProviderEconomicsHarness(
            LiveBatchGate(
                campaign=campaign,
                provider_execution_enabled=True,
                external_cost_acknowledgement=provider_acknowledgement(campaign.campaign_id),
                readiness=attestation,
            )
        ).dry_run()
    except (RuntimeError, ValueError, TypeError):
        raise ReadinessBlocked("provider_readiness_attestation_invalid") from None
    blockers = []
    if not arm_live_provider:
        blockers.append("explicit --arm-live-provider flag is absent")
    if environment.get(ARM_ENVIRONMENT_VARIABLE) != campaign.campaign_id:
        blockers.append(f"{ARM_ENVIRONMENT_VARIABLE} does not equal {campaign.campaign_id}")
    armed = not blockers
    return ReadinessResult(
        campaign_id=campaign.campaign_id,
        tenant_id=campaign.tenant_id,
        configuration_ready=True,
        armed=armed,
        execution_ready=armed,
        blockers=tuple(blockers),
    )


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage()
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="live-provider-gate")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_SafeParser)
    command = commands.add_parser("readiness")
    command.add_argument("--campaign-config", type=Path, required=True)
    command.add_argument("--readiness-attestation", type=Path, required=True)
    command.add_argument("--wiring-config", type=Path, required=True)
    command.add_argument("--arm-live-provider", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        campaign = CampaignConfig.model_validate(
            _object(args.campaign_config, "campaign_configuration_invalid")
        )
        attestation = ReadinessAttestation.from_mapping(
            _object(args.readiness_attestation, "provider_readiness_attestation_invalid")
        )
        wiring = _parse_wiring(_object(args.wiring_config, "wiring_configuration_invalid"))
        result = readiness(
            campaign=campaign,
            attestation=attestation,
            wiring=wiring,
            arm_live_provider=args.arm_live_provider,
            environment=os.environ,
        )
    except ReadinessBlocked as exc:
        print(
            json.dumps(
                {
                    "configuration_ready": False,
                    "provider_calls_performed": 0,
                    "reason": exc.code,
                },
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "configuration_ready": False,
                    "provider_calls_performed": 0,
                    "reason": "readiness_configuration_invalid",
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARM_ENVIRONMENT_VARIABLE",
    "PLANNED_ORDER",
    "ProviderFreeWiring",
    "ReadinessBlocked",
    "ReadinessResult",
    "build_parser",
    "main",
    "readiness",
]
