"""Build the unified live-provider wiring document without touching runtime planes."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .batch_provider_economics import MAX_CAMPAIGN_USD, MAX_PER_RUN_USD
from .config import CampaignConfig
from .live_provider_gate import _loopback_origin
from .native_safari_rightsizing_snapshot import write_json_atomic
from .rightsizing_live_checkpoint import load_recorded_cases
from .rightsizing_service_adapter import ExperimentRequest
from .template_live_rendered_execution import LiveTemplateConfig, LiveTemplateFixture
from .template_provisioning_cli import EXACT_CONFIG, FROZEN_D012_IDENTITY

_PROVISIONING_KEYS = {
    "status",
    "config",
    "pre_health",
    "post_health",
    "fixture",
    "provider_calls_performed",
}
_CONFIG_KEYS = {"fixture_id", "tenant_id", "template_name", "deployment_ref"}
_FIXTURE_KEYS = {
    "fixture_id",
    "template_name",
    "template_version",
    "workflow_id",
    "graph_version_ref",
    "deployment_ref",
    "deployment_version",
    "provider_calls_performed",
}
_SECRET_SHAPED = re.compile(
    r'"(?:authorization|api[_-]?key|service[_-]?key|provider[_-]?key)"\s*:'
    r"|bearer\s+\S+|\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}",
    re.IGNORECASE,
)
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is missing or unsafe") from exc
    if len(payload) > 2_000_000:
        raise ValueError(f"{label} is too large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8 JSON") from exc
    if _SECRET_SHAPED.search(text):
        raise ValueError(f"{label} contains secret-shaped content")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _safe_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"{label} is missing or unsafe")
    return candidate.resolve(strict=True)


def _persistent_file(path: Path, *, root: Path, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"persistent path {label} is missing or unsafe")
    cursor = candidate.absolute()
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise ValueError(f"persistent path {label} is missing or unsafe")
        if cursor == root:
            break
        cursor = cursor.parent
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"persistent path {label} is outside the campaign root")
    return resolved


def _campaign(path: Path) -> CampaignConfig:
    raw = _json_object(path, label="campaign configuration")
    try:
        campaign = CampaignConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("campaign configuration is invalid") from exc
    if campaign.campaign_id != campaign.tenant_id:
        raise ValueError("campaign and tenant identities must match")
    if campaign.campaign_budget_usd != MAX_CAMPAIGN_USD:
        raise ValueError("campaign budget must be exactly $10.00")
    if campaign.per_run_cap_usd != MAX_PER_RUN_USD:
        raise ValueError("per-run cap must be exactly $0.25")
    if campaign.provider_secret_ref != "llm.openai":
        raise ValueError("campaign provider reference is not the approved logical reference")
    return campaign


def _batch_items(path: Path) -> list[dict[str, object]]:
    raw = _json_object(path, label="batch fixture")
    if set(raw) != {"schema_version", "items"} or raw.get("schema_version") != 1:
        raise ValueError("batch fixture document contract is invalid")
    rows = raw.get("items")
    if not isinstance(rows, list) or len(rows) != 8:
        raise ValueError("batch fixture must contain exactly eight items")
    items: list[dict[str, object]] = []
    for expected_index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"index", "query"}:
            raise ValueError("batch fixture item contract is invalid")
        index = row.get("index")
        query = row.get("query")
        if (
            isinstance(index, bool)
            or index != expected_index
            or not isinstance(query, str)
            or query != query.strip()
            or not 20 <= len(query) <= 2_000
            or len(query.split()) < 3
            or any(ord(character) < 32 for character in query)
        ):
            raise ValueError("batch fixture item is not a real-world-like indexed query")
        items.append({"index": expected_index, "query": query})
    return items


def _template(path: Path, *, campaign: CampaignConfig) -> dict[str, object]:
    raw = _json_object(path, label="template fixture")
    if set(raw) != _PROVISIONING_KEYS:
        raise ValueError("template provisioning result field contract is invalid")
    config_raw = raw.get("config")
    fixture_raw = raw.get("fixture")
    pre_health = raw.get("pre_health")
    post_health = raw.get("post_health")
    exact_config = asdict(EXACT_CONFIG)
    exact_health = {"status": "ok", **FROZEN_D012_IDENTITY}
    if (
        raw.get("status") != "provisioned"
        or raw.get("provider_calls_performed") != 0
        or isinstance(raw.get("provider_calls_performed"), bool)
        or not isinstance(config_raw, dict)
        or set(config_raw) != _CONFIG_KEYS
        or config_raw != exact_config
        or config_raw.get("tenant_id") != campaign.tenant_id
        or not isinstance(pre_health, dict)
        or not isinstance(post_health, dict)
        or pre_health != exact_health
        or post_health != exact_health
        or pre_health != post_health
        or not isinstance(fixture_raw, dict)
        or set(fixture_raw) != _FIXTURE_KEYS
    ):
        raise ValueError("template provisioning result identity or health is invalid")
    try:
        fixture = LiveTemplateFixture(**fixture_raw)
        config = LiveTemplateConfig(**config_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("template provisioning result fixture is invalid") from exc
    if (
        fixture.fixture_id != config.fixture_id
        or fixture.template_name != config.template_name
        or fixture.deployment_ref != config.deployment_ref
        or fixture.template_version != 1
        or isinstance(fixture.template_version, bool)
        or fixture.deployment_version != 1
        or isinstance(fixture.deployment_version, bool)
        or fixture.provider_calls_performed != 0
        or isinstance(fixture.provider_calls_performed, bool)
        or not isinstance(fixture.workflow_id, str)
        or not _SAFE_SLUG.fullmatch(fixture.workflow_id)
        or fixture.graph_version_ref != f"{fixture.workflow_id}@1"
    ):
        raise ValueError("template provisioning result is not an unexecuted version-one fixture")
    return {**asdict(fixture), **asdict(config)}


def _rightsizing(path: Path, *, campaign: CampaignConfig) -> dict[str, object]:
    # Scan before delegating to the existing strict case parser and byte digest.
    _json_object(path, label="rightsizing cases")
    try:
        cases, digest = load_recorded_cases(path)
    except (OSError, ValueError) as exc:
        raise ValueError("rightsizing cases are invalid") from exc
    if len(cases) > 25:
        raise ValueError("rightsizing cases exceed the measured endpoint limit")
    request = ExperimentRequest(
        node_id="research-agent",
        incumbent=campaign.model,
        instruction="Answer only from supplied context.",
        needs_tools=False,
        needs_vision=False,
        judge_model=campaign.model,
        max_candidates=1,
        max_cases=len(cases),
        min_cases=len(cases),
        tolerance_pct=5.0,
        mode="equivalence",
    )
    return {"cases_sha256": digest, **request.payload()}


def build_wiring(
    *,
    campaign_config: Path,
    service_database: Path,
    economics_database: Path,
    action_sink_database: Path,
    provider_window: Path,
    service_base_url: str,
    template_fixture: Path,
    batch_fixture: Path,
    rightsizing_cases: Path,
) -> dict[str, object]:
    """Return an armable wiring document while performing only bounded file reads."""
    campaign = _campaign(campaign_config)
    artifact_root = _safe_directory(campaign.artifact_root, label="campaign artifact root")
    action_root = _safe_directory(campaign.action_sink_root, label="action sink root")
    service = _persistent_file(service_database, root=artifact_root, label="service database")
    economics = _persistent_file(economics_database, root=artifact_root, label="economics database")
    action = _persistent_file(action_sink_database, root=action_root, label="action sink database")
    provider = _persistent_file(provider_window, root=artifact_root, label="provider window")
    if len({service, economics, action, provider}) != 4:
        raise ValueError("persistent paths must identify four distinct files")
    _json_object(provider, label="provider window")
    wiring: dict[str, object] = {
        "schema_version": 1,
        "service_base_url": _loopback_origin(service_base_url),
        "service_database": str(service),
        "econ_database": str(economics),
        "action_sink_database": str(action),
        "provider_window": str(provider),
        "batch_items": _batch_items(batch_fixture),
        "template": _template(template_fixture, campaign=campaign),
        "rightsizing": _rightsizing(rightsizing_cases, campaign=campaign),
    }
    # Reuse the actual consumer as the final schema/type gate. It only stats the
    # already validated persistent paths and performs no DB, HTTP, or provider call.
    _parse_for_consumer(wiring)
    return wiring


def _parse_for_consumer(wiring: Mapping[str, object]) -> None:
    from .live_provider_gate import _parse_wiring

    _parse_wiring(wiring)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-provider-wiring-builder")
    parser.add_argument("--campaign-config", required=True, type=Path)
    parser.add_argument("--service-db", required=True, type=Path)
    parser.add_argument("--econ-db", required=True, type=Path)
    parser.add_argument("--action-sink-db", required=True, type=Path)
    parser.add_argument("--provider-window", required=True, type=Path)
    parser.add_argument("--service-base-url", required=True)
    parser.add_argument("--template-fixture", required=True, type=Path)
    parser.add_argument("--batch-fixture", required=True, type=Path)
    parser.add_argument("--rightsizing-cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wiring = build_wiring(
        campaign_config=args.campaign_config,
        service_database=args.service_db,
        economics_database=args.econ_db,
        action_sink_database=args.action_sink_db,
        provider_window=args.provider_window,
        service_base_url=args.service_base_url,
        template_fixture=args.template_fixture,
        batch_fixture=args.batch_fixture,
        rightsizing_cases=args.rightsizing_cases,
    )
    write_json_atomic(args.output, wiring)
    print(json.dumps({"created": str(args.output), "provider_calls_performed": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "build_wiring", "main"]
