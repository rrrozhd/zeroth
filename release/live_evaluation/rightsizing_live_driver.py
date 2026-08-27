"""One-shot measured Rightsizing driver over the approved public service route.

This command composes the provider-free wiring gate, the service HTTP adapter,
and authoritative persisted-plane reconciliation.  It can read only the Zeroth
service API key; the provider credential remains behind the running service's
logical ``llm.openai`` reference.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .config import CampaignConfig
from .live_provider_gate import (
    ARM_ENVIRONMENT_VARIABLE,
    ProviderFreeWiring,
    ReadinessBlocked,
    _object,
    _parse_wiring,
)
from .reconciliation_export import AuthoritativeExportBlocked, HttpAuditSource
from .rightsizing_authoritative_reconciliation import (
    AuthoritativeRightsizingReconciliationCollector,
)
from .rightsizing_live_checkpoint import (
    ARM_PHRASE,
    CAMPAIGN_CAP_USD,
    DEFAULT_CASES,
    PER_RUN_CAP_USD,
    ServiceRightsizingCapture,
    load_recorded_cases,
    validate_service_capture,
)
from .rightsizing_service_adapter import RightsizingServiceAdapter
from .template_provisioning_cli import (
    ProvisioningBlockedError,
    validate_service_api_key_file,
)

_SECRET_SHAPED = re.compile(
    r'"(?:authorization|api[_-]?key|service[_-]?key|provider[_-]?key|secret|password|credential)"\s*:'
    r"|bearer\s+\S+|\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}",
    re.IGNORECASE,
)


class RightsizingDriverBlockedError(RuntimeError):
    """A stable non-sensitive reason the one-shot driver refused to proceed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RightsizingExecutionContract:
    """Explicit cardinality and evidence contract for one bounded live experiment."""

    node_id: str
    cases_sha256: str
    max_cases: int
    min_cases: int
    expected_provider_calls: int
    required_verdict: Literal["confirmed", "flagged", "none"]

    def __post_init__(self) -> None:
        if (
            not self.node_id
            or len(self.cases_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.cases_sha256)
            or isinstance(self.max_cases, bool)
            or not 1 <= self.max_cases <= 25
            or isinstance(self.min_cases, bool)
            or not 1 <= self.min_cases <= 50
            or isinstance(self.expected_provider_calls, bool)
            or not 1 <= self.expected_provider_calls <= 100
        ):
            raise ValueError("invalid Rightsizing execution contract")


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage()
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


class _ServiceKeySource:
    """Read one private service credential without retaining its value."""

    def __init__(self, path: Path) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        try:
            self._path = validate_service_api_key_file(path, repository_root=repository_root)
        except ProvisioningBlockedError as exc:
            raise RightsizingDriverBlockedError(exc.args[0]) from None

    def __call__(self) -> str:
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            raise RightsizingDriverBlockedError("service-key-file-unreadable") from None
        if not value or "\n" in value or "\r" in value or len(value) > 4096:
            raise RightsizingDriverBlockedError("service-key-file-invalid")
        return value


class _ServiceDatabaseAuditSource:
    """Resolve exact run deployments from SQLite, then use signed public APIs."""

    def __init__(
        self,
        *,
        service_database: Path,
        base_url: str,
        tenant_id: str,
        campaign_id: str,
        auth_source: _ServiceKeySource,
    ) -> None:
        self._service_database = service_database
        self._base_url = base_url
        self._tenant_id = tenant_id
        self._campaign_id = campaign_id
        self._auth_source = auth_source
        self._sources: dict[tuple[str, ...], HttpAuditSource] = {}

    def _source(self, run_ids: tuple[str, ...]) -> HttpAuditSource:
        key = tuple(sorted(run_ids))
        existing = self._sources.get(key)
        if existing is not None:
            return existing
        if not key:
            raise AuthoritativeExportBlocked("campaign_run_inventory_empty")
        try:
            with sqlite3.connect(
                f"file:{self._service_database}?mode=ro", uri=True
            ) as database:
                database.row_factory = sqlite3.Row
                placeholders = ",".join("?" for _ in key)
                rows = database.execute(
                    f"""SELECT run_id, deployment_ref, tenant_id, metadata FROM runs
                    WHERE tenant_id = ? AND run_id IN ({placeholders})""",
                    (self._tenant_id, *key),
                ).fetchall()
        except sqlite3.DatabaseError:
            raise AuthoritativeExportBlocked("service_run_inventory_invalid") from None
        if len(rows) != len(key) or {str(row["run_id"]) for row in rows} != set(key):
            raise AuthoritativeExportBlocked("campaign_run_inventory_incomplete")
        deployments: dict[str, str] = {}
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata"]))
            except (TypeError, json.JSONDecodeError):
                raise AuthoritativeExportBlocked("service_run_identity_invalid") from None
            deployment_ref = row["deployment_ref"]
            if (
                row["tenant_id"] != self._tenant_id
                or not isinstance(metadata, dict)
                or metadata.get("campaign_id") != self._campaign_id
                or not isinstance(deployment_ref, str)
                or not deployment_ref
            ):
                raise AuthoritativeExportBlocked("service_run_identity_invalid")
            deployments[deployment_ref] = self._base_url
        try:
            service_key = self._auth_source()
        except RightsizingDriverBlockedError:
            raise AuthoritativeExportBlocked("audit_api_credential_missing") from None
        source = HttpAuditSource(
            deployments=deployments,
            headers={"X-API-Key": service_key, "X-Tenant-ID": self._tenant_id},
        )
        service_key = ""
        self._sources[key] = source
        return source

    def records_for_runs(self, run_ids: tuple[str, ...]):
        return self._source(run_ids).records_for_runs(run_ids)

    def verify_run(self, run_id: str):
        return self._source((run_id,)).verify_run(run_id)

    def close(self) -> None:
        for source in self._sources.values():
            source.headers.clear()
            source.client.close()
        self._sources.clear()


def _money(value: object, code: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RightsizingDriverBlockedError(code) from None
    if not amount.is_finite() or amount < 0:
        raise RightsizingDriverBlockedError(code)
    return amount


def _campaign_spend(path: Path, *, campaign_id: str, tenant_id: str) -> Decimal:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                """SELECT status, actual_cost_usd, held_cost_usd FROM cost_reservations
                WHERE tenant_id = ? AND campaign_id = ?""",
                (tenant_id, campaign_id),
            ).fetchall()
    except sqlite3.DatabaseError:
        raise RightsizingDriverBlockedError("campaign_spend_unavailable") from None
    total = Decimal("0")
    for row in rows:
        status = row["status"]
        if status == "committed":
            total += _money(row["actual_cost_usd"], "campaign_spend_invalid")
        elif status in {"reserved", "ambiguous"}:
            total += _money(row["held_cost_usd"], "campaign_spend_invalid")
        elif status != "released":
            raise RightsizingDriverBlockedError("campaign_spend_invalid")
    if total > CAMPAIGN_CAP_USD:
        raise RightsizingDriverBlockedError("campaign_cap_already_exceeded")
    return total


def _default_contract() -> RightsizingExecutionContract:
    cases, digest = load_recorded_cases(DEFAULT_CASES)
    return RightsizingExecutionContract(
        node_id="research-agent",
        cases_sha256=digest,
        max_cases=len(cases),
        min_cases=len(cases),
        expected_provider_calls=len(cases) * 4,
        required_verdict="confirmed",
    )


def _assert_contract(
    campaign: CampaignConfig,
    wiring: ProviderFreeWiring,
    *,
    contract: RightsizingExecutionContract | None = None,
) -> None:
    if (
        campaign.campaign_id != campaign.tenant_id
        or campaign.campaign_budget_usd != CAMPAIGN_CAP_USD
        or campaign.per_run_cap_usd != PER_RUN_CAP_USD
        or campaign.provider_secret_ref != "llm.openai"
    ):
        raise RightsizingDriverBlockedError("campaign_contract_invalid")
    request = wiring.rightsizing_request
    expected = contract or _default_contract()
    if (
        request.incumbent != campaign.model
        or request.judge_model != campaign.model
        or request.max_candidates != 1
        or request.node_id != expected.node_id
        or request.max_cases != expected.max_cases
        or request.min_cases != expected.min_cases
        or request.mode != "equivalence"
        or wiring.rightsizing_cases_sha256 != expected.cases_sha256
    ):
        raise RightsizingDriverBlockedError("rightsizing_wiring_invalid")


def _assert_capture_contract(
    capture: ServiceRightsizingCapture,
    contract: RightsizingExecutionContract,
) -> None:
    if (
        capture.node_id != contract.node_id
        or capture.cases != contract.max_cases
        or capture.min_cases != contract.min_cases
        or capture.verdict != contract.required_verdict
    ):
        raise RightsizingDriverBlockedError("rightsizing_result_contract_invalid")
    if len(capture.calls) != contract.expected_provider_calls:
        raise RightsizingDriverBlockedError("rightsizing_call_count_invalid")


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(child) for child in value]
    return value


def _sum(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0"))


def _observation(
    capture: ServiceRightsizingCapture,
    *,
    authoritative_campaign_spend_after_usd: Decimal,
) -> dict[str, object]:
    summary = validate_service_capture(capture)
    reconciliation = capture.reconciliation
    accounted = _sum(
        tuple(
            call.measured_cost_usd
            if call.measured_cost_usd is not None
            else call.estimated_cost_usd or Decimal("0")
            for call in capture.calls
        )
    )
    conservative = _sum(
        tuple(
            max(
                call.measured_cost_usd or Decimal("0"),
                call.estimated_cost_usd or Decimal("0"),
            )
            for call in capture.calls
        )
    )
    expected_after = capture.prior_campaign_spend_usd + conservative
    after = max(expected_after, authoritative_campaign_spend_after_usd)
    if conservative > PER_RUN_CAP_USD or after > CAMPAIGN_CAP_USD:
        raise RightsizingDriverBlockedError("post_response_cost_cap_exceeded")
    return {
        "schema_version": 1,
        "status": "verified",
        "criteria": {
            "rightsizing.measured-experiment": "pass",
            "rightsizing.cost-reconciliation": "pass",
        },
        "campaign_id": capture.campaign_id,
        "run_id": capture.run_id,
        "cases_sha256": capture.cases_sha256,
        "experiment": {
            "node_id": capture.node_id,
            "mode": capture.mode,
            "cases": capture.cases,
            "min_cases": capture.min_cases,
            "verdict": capture.verdict,
            "recommended_model": capture.recommended_model,
            "calls": [_jsonable(asdict(call)) for call in capture.calls],
            "outcomes": [_jsonable(asdict(outcome)) for outcome in capture.outcomes],
        },
        "economics": {
            "campaign_cap_usd": format(CAMPAIGN_CAP_USD, "f"),
            "per_run_cap_usd": format(PER_RUN_CAP_USD, "f"),
            "campaign_spend_before_usd": format(capture.prior_campaign_spend_usd, "f"),
            "campaign_spend_after_usd": format(after, "f"),
            # The endpoint emits every replay/judge call but intentionally does not
            # expose internal roles.  Reconcile the complete set without inventing
            # a candidate-versus-judge split.
            "experiment_replay_and_judge_total_usd": format(accounted, "f"),
            "response_measured_total_usd": format(summary.measured_total_usd, "f"),
            "response_estimated_total_usd": format(summary.estimated_total_usd, "f"),
            "audit_total_usd": format(
                _sum(tuple(row.cost_usd for row in reconciliation.audits)), "f"
            ),
            "reservation_committed_count": sum(
                row.state == "committed" for row in reconciliation.reservations
            ),
            "local_cost_total_usd": format(
                _sum(tuple(row.amount_usd for row in reconciliation.local_cost_events)), "f"
            ),
            "regulus_total_usd": format(
                _sum(tuple(row.amount_usd for row in reconciliation.regulus_events)), "f"
            ),
            "provider_window_policy": summary.provider_window_policy,
            "role_attribution": "public_endpoint_unavailable",
            "tolerance": "max(0.000001 USD, 0.5 percent)",
        },
        "reconciliation": {
            "audits": [_jsonable(asdict(row)) for row in reconciliation.audits],
            "reservations": [_jsonable(asdict(row)) for row in reconciliation.reservations],
            "local_cost_events": [
                _jsonable(asdict(row)) for row in reconciliation.local_cost_events
            ],
            "regulus_events": [_jsonable(asdict(row)) for row in reconciliation.regulus_events],
            "provider_window": _jsonable(asdict(reconciliation.provider_window)),
        },
    }


def _write_observation(destination: Path, value: Mapping[str, object]) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if _SECRET_SHAPED.search(encoded.decode("utf-8")):
        raise RightsizingDriverBlockedError("observation_contains_secret_shaped_content")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    except FileExistsError:
        raise RightsizingDriverBlockedError("observation_already_exists") from None
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def execute(
    *,
    campaign: CampaignConfig,
    wiring: ProviderFreeWiring,
    service_api_key_file: Path,
    output: Path,
    arm: str,
    environment: Mapping[str, str],
    adapter: RightsizingServiceAdapter | None = None,
    contract: RightsizingExecutionContract | None = None,
) -> Path:
    """Execute exactly one armed public experiment and write sanitized evidence."""
    if arm != ARM_PHRASE:
        raise RightsizingDriverBlockedError("live_execution_not_armed")
    if environment.get(ARM_ENVIRONMENT_VARIABLE) != campaign.campaign_id:
        raise RightsizingDriverBlockedError("live_environment_not_armed")
    _assert_contract(campaign, wiring, contract=contract)
    prior_spend = _campaign_spend(
        wiring.econ_database,
        campaign_id=campaign.campaign_id,
        tenant_id=campaign.tenant_id,
    )
    if prior_spend + PER_RUN_CAP_USD > CAMPAIGN_CAP_USD:
        raise RightsizingDriverBlockedError("campaign_capacity_insufficient")
    auth_source = _ServiceKeySource(service_api_key_file)
    audit_source = _ServiceDatabaseAuditSource(
        service_database=wiring.service_database,
        base_url=wiring.service_base_url,
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.campaign_id,
        auth_source=auth_source,
    )
    collector = AuthoritativeRightsizingReconciliationCollector(
        tenant_id=campaign.tenant_id,
        econ_database=wiring.econ_database,
        action_sink_database=wiring.action_sink_database,
        audit_source=audit_source,
        provider_window=wiring.provider_window,
    )
    subject = adapter or RightsizingServiceAdapter(base_url=wiring.service_base_url)
    try:
        capture = subject.collect(
            request=wiring.rightsizing_request,
            tenant_id=campaign.tenant_id,
            cases_sha256=wiring.rightsizing_cases_sha256,
            prior_campaign_spend_usd=prior_spend,
            arm=arm,
            provider_ready=lambda: True,
            auth_source=auth_source,
            reconciliation_collector=collector,
        )
    finally:
        audit_source.close()
    if (
        capture.campaign_id != campaign.campaign_id
        or capture.cases_sha256 != wiring.rightsizing_cases_sha256
        or capture.prior_campaign_spend_usd != prior_spend
    ):
        raise RightsizingDriverBlockedError("captured_identity_mismatch")
    if contract is not None:
        _assert_capture_contract(capture, contract)
    authoritative_after = _campaign_spend(
        wiring.econ_database,
        campaign_id=campaign.campaign_id,
        tenant_id=campaign.tenant_id,
    )
    return _write_observation(
        output,
        _observation(
            capture,
            authoritative_campaign_spend_after_usd=authoritative_after,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="rightsizing-live-driver")
    parser.add_argument("--campaign-config", required=True, type=Path)
    parser.add_argument("--wiring-config", required=True, type=Path)
    parser.add_argument("--service-api-key-file", required=True, type=Path)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        campaign = CampaignConfig.model_validate(
            _object(args.campaign_config, "campaign_configuration_invalid")
        )
        wiring = _parse_wiring(_object(args.wiring_config, "wiring_configuration_invalid"))
        destination = execute(
            campaign=campaign,
            wiring=wiring,
            service_api_key_file=args.service_api_key_file,
            output=args.output,
            arm=args.arm,
            environment=os.environ,
        )
    except RightsizingDriverBlockedError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code}, sort_keys=True))
        return 2
    except (ReadinessBlocked, ValidationError, ValueError, TypeError, OSError):
        print(
            json.dumps(
                {"status": "blocked", "reason": "rightsizing_driver_validation_failed"},
                sort_keys=True,
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"status": "blocked", "reason": "rightsizing_driver_runtime_failed"},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps({"status": "verified", "observation": str(destination)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RightsizingDriverBlockedError",
    "RightsizingExecutionContract",
    "build_parser",
    "execute",
    "main",
]
