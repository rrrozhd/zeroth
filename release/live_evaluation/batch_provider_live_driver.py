"""Fail-closed live driver for the provider-backed batch economics checkpoint.

The command composes the already-reviewed public-service adapter, authoritative
repository collector, and economics harness.  Its only credential input is a
private Zeroth service-key file; provider credentials remain owned by the
running service and are never accepted or resolved here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from .batch_provider_economics import (
    MAX_CAMPAIGN_USD,
    MAX_PER_RUN_USD,
    BatchProviderEconomicsHarness,
    LiveBatchAdapter,
    LiveBatchGate,
    ReadinessAttestation,
)
from .batch_provider_repository_collector import RepositoryBackedBatchCollector
from .batch_provider_service_adapter import ARM_PHRASE, BatchProviderServiceAdapter
from .campaign_http import provider_acknowledgement
from .config import CampaignConfig
from .live_provider_gate import (
    ARM_ENVIRONMENT_VARIABLE,
    ProviderFreeWiring,
    _parse_wiring,
    readiness,
)
from .live_provider_wiring_builder import _json_object
from .native_safari_rightsizing_snapshot import write_json_atomic


class BatchProviderLiveBlocked(RuntimeError):  # noqa: N818
    """Stable, non-sensitive execution failure for operator automation."""

    def __init__(self, code: str, *, provider_calls_performed: int | None = 0) -> None:
        self.code = code
        self.provider_calls_performed = provider_calls_performed
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedBatchProviderExecution:
    campaign: CampaignConfig
    readiness_attestation: ReadinessAttestation
    wiring: ProviderFreeWiring
    service_api_key_file: Path
    arm: str


class AdapterFactory(Protocol):
    def __call__(
        self,
        prepared: PreparedBatchProviderExecution,
        auth_source: Callable[[], str],
    ) -> LiveBatchAdapter: ...


def _service_key_path(path: Path) -> Path:
    repository_root = Path(__file__).resolve().parents[2]
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise BatchProviderLiveBlocked("service_key_file_not_exact")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise BatchProviderLiveBlocked("service_key_file_unavailable") from exc
    if resolved.is_relative_to(repository):
        raise BatchProviderLiveBlocked("service_key_file_inside_repository")
    if not stat.S_ISREG(metadata.st_mode):
        raise BatchProviderLiveBlocked("service_key_file_not_regular")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BatchProviderLiveBlocked("service_key_file_not_private")
    return resolved


class _ServiceKeySource:
    """Read the explicitly supplied service key only at authenticated call time."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self) -> str:
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise PermissionError("service authentication is unavailable") from exc
        if not value or "\n" in value or "\r" in value or len(value) > 4096:
            raise PermissionError("service authentication is unavailable")
        return value


def _contains_credential(value: object, credential: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_credential(child, credential) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_credential(child, credential) for child in value)
    if not isinstance(value, str):
        return False
    return value == credential or (len(credential) >= 16 and credential in value)


class _PublicRunAuditSource:
    """Read child audits and chain verdicts from run-scoped public APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        tenant_id: str,
        campaign_id: str,
        auth_source: Callable[[], str],
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url
        self._tenant_id = tenant_id
        self._campaign_id = campaign_id
        self._auth_source = auth_source
        self._client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._records: dict[str, tuple[dict[str, object], ...]] = {}
        self._verifications: dict[str, dict[str, object]] = {}

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, payload: Mapping[str, object] | None = None):
        try:
            service_key = self._auth_source()
            headers = {
                "Accept": "application/json",
                "X-API-Key": service_key,
                "X-Tenant-ID": self._tenant_id,
            }
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=payload,
            )
        except Exception:
            raise BatchProviderLiveBlocked(
                "authoritative_audit_api_unavailable",
                provider_calls_performed=None,
            ) from None
        finally:
            service_key = ""
            if "headers" in locals():
                headers.clear()
        if response.status_code != 200:
            raise BatchProviderLiveBlocked(
                "authoritative_audit_api_unavailable",
                provider_calls_performed=None,
            )
        try:
            return response.json()
        except Exception:
            raise BatchProviderLiveBlocked(
                "authoritative_audit_api_invalid",
                provider_calls_performed=None,
            ) from None

    def _load(self, run_id: str) -> None:
        if run_id in self._records:
            return
        escaped = quote(run_id, safe="")
        evidence = self._request("GET", f"/v1/runs/{escaped}/evidence")
        verification = self._request(
            "POST", f"/v1/runs/{escaped}/verify-chain", payload={}
        )
        audits = evidence.get("audits") if isinstance(evidence, Mapping) else None
        run = evidence.get("run") if isinstance(evidence, Mapping) else None
        if (
            not isinstance(run, Mapping)
            or run.get("run_id") != run_id
            or run.get("campaign_id") != self._campaign_id
            or not isinstance(audits, list)
            or not audits
            or not all(isinstance(row, dict) for row in audits)
            or not isinstance(verification, dict)
        ):
            raise BatchProviderLiveBlocked(
                "authoritative_audit_api_invalid",
                provider_calls_performed=None,
            )
        self._records[run_id] = tuple(dict(row) for row in audits)
        self._verifications[run_id] = dict(verification)

    def records_for_runs(self, run_ids: tuple[str, ...]):
        for run_id in run_ids:
            self._load(run_id)
        return tuple(row for run_id in run_ids for row in self._records[run_id])

    def verify_run(self, run_id: str):
        self._load(run_id)
        value = dict(self._verifications[run_id])
        value.setdefault("run_id", run_id)
        value.setdefault("tenant_id", self._tenant_id)
        value.setdefault("campaign_id", self._campaign_id)
        return value


def prepare(
    *,
    campaign_config: Path,
    readiness_attestation: Path,
    wiring_config: Path,
    service_api_key_file: Path,
    output: Path,
    arm: str,
    environment: Mapping[str, str],
) -> PreparedBatchProviderExecution:
    """Validate every non-provider precondition before the service key is read."""
    if arm != ARM_PHRASE or environment.get(ARM_ENVIRONMENT_VARIABLE) is None:
        raise BatchProviderLiveBlocked("operator_interlock_invalid")
    destination = output.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    try:
        campaign = CampaignConfig.model_validate(
            _json_object(campaign_config, label="campaign configuration")
        )
        attestation = ReadinessAttestation.from_mapping(
            _json_object(readiness_attestation, label="readiness attestation")
        )
        wiring = _parse_wiring(_json_object(wiring_config, label="approved wiring"))
    except Exception as exc:
        raise BatchProviderLiveBlocked("campaign_configuration_invalid") from exc
    if (
        campaign.campaign_budget_usd != MAX_CAMPAIGN_USD
        or campaign.per_run_cap_usd != MAX_PER_RUN_USD
    ):
        raise BatchProviderLiveBlocked("campaign_configuration_invalid")
    if environment.get(ARM_ENVIRONMENT_VARIABLE) != campaign.campaign_id:
        raise BatchProviderLiveBlocked("operator_interlock_invalid")
    try:
        readiness_result = readiness(
            campaign=campaign,
            attestation=attestation,
            wiring=wiring,
            arm_live_provider=True,
            environment=environment,
        )
    except Exception as exc:
        raise BatchProviderLiveBlocked("readiness_attestation_invalid") from exc
    if not readiness_result.execution_ready:
        raise BatchProviderLiveBlocked("operator_interlock_invalid")
    key_path = _service_key_path(service_api_key_file)
    return PreparedBatchProviderExecution(
        campaign=campaign,
        readiness_attestation=attestation,
        wiring=wiring,
        service_api_key_file=key_path,
        arm=arm,
    )


def _default_adapter_factory(
    prepared: PreparedBatchProviderExecution,
    auth_source: Callable[[], str],
) -> LiveBatchAdapter:
    audit_source = _PublicRunAuditSource(
        base_url=prepared.wiring.service_base_url,
        tenant_id=prepared.campaign.tenant_id,
        campaign_id=prepared.campaign.campaign_id,
        auth_source=auth_source,
    )
    collector = RepositoryBackedBatchCollector(
        service_database=prepared.wiring.service_database,
        econ_database=prepared.wiring.econ_database,
        audit_source=audit_source,
    )
    return BatchProviderServiceAdapter(
        base_url=prepared.wiring.service_base_url,
        tenant_id=prepared.campaign.tenant_id,
        items=prepared.wiring.batch_items,
        arm=prepared.arm,
        provider_ready=lambda: True,
        auth_source=auth_source,
        reconciliation_collector=collector,
    )


async def _execute_async(
    prepared: PreparedBatchProviderExecution,
    *,
    adapter_factory: AdapterFactory,
):
    auth_source = _ServiceKeySource(prepared.service_api_key_file)
    adapter = adapter_factory(prepared, auth_source)
    gate = LiveBatchGate(
        campaign=prepared.campaign,
        provider_execution_enabled=True,
        external_cost_acknowledgement=provider_acknowledgement(
            prepared.campaign.campaign_id
        ),
        readiness=prepared.readiness_attestation,
    )
    try:
        result = await BatchProviderEconomicsHarness(gate).execute(adapter)
        service_key = auth_source()
        try:
            if _contains_credential(result.as_dict(), service_key):
                raise BatchProviderLiveBlocked(
                    "observation_contains_service_credential",
                    provider_calls_performed=24,
                )
        finally:
            service_key = ""
        return result
    finally:
        collector = getattr(adapter, "_collector", None)
        audit_source = getattr(collector, "audit_source", None)
        close = getattr(audit_source, "close", None)
        if callable(close):
            close()


def execute(
    *,
    campaign_config: Path,
    readiness_attestation: Path,
    wiring_config: Path,
    service_api_key_file: Path,
    output: Path,
    arm: str,
    environment: Mapping[str, str],
    adapter_factory: AdapterFactory = _default_adapter_factory,
) -> Path:
    """Execute three real parents and atomically create the reconciled observation."""
    prepared = prepare(
        campaign_config=campaign_config,
        readiness_attestation=readiness_attestation,
        wiring_config=wiring_config,
        service_api_key_file=service_api_key_file,
        output=output,
        arm=arm,
        environment=environment,
    )
    try:
        result = asyncio.run(_execute_async(prepared, adapter_factory=adapter_factory))
    except BatchProviderLiveBlocked:
        raise
    except Exception as exc:
        raise BatchProviderLiveBlocked(
            "live_batch_execution_failed", provider_calls_performed=None
        ) from exc
    return write_json_atomic(output, result.as_dict())


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage()
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="batch-provider-live-driver")
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--readiness-attestation", type=Path, required=True)
    parser.add_argument("--wiring-config", type=Path, required=True)
    parser.add_argument("--service-api-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = execute(
            campaign_config=args.campaign_config,
            readiness_attestation=args.readiness_attestation,
            wiring_config=args.wiring_config,
            service_api_key_file=args.service_api_key_file,
            output=args.output,
            arm=args.arm,
            environment=os.environ,
        )
    except BatchProviderLiveBlocked as exc:
        print(
            json.dumps(
                {
                    "completed": False,
                    "provider_calls_performed": exc.provider_calls_performed,
                    "reason": exc.code,
                },
                sort_keys=True,
            )
        )
        return 2
    except FileExistsError:
        print(
            json.dumps(
                {
                    "completed": False,
                    "provider_calls_performed": 0,
                    "reason": "output_already_exists",
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "completed": True,
                "observation": str(destination),
                "provider_calls_performed": 24,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BatchProviderLiveBlocked",
    "PreparedBatchProviderExecution",
    "build_parser",
    "execute",
    "main",
    "prepare",
]
