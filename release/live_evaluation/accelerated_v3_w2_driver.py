"""Bounded Workflow 2 repetition-three driver for accelerated demo V3.

This module intentionally cannot run the historical three-parent batch plan.  It
creates one repetition-three parent only, after the V3 authorization phrase and
the checksum-sealed Workflow 1 remediation prerequisite are verified.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .batch_provider_economics import (
    CONCURRENCY,
    CRITERION_ID,
    ITEMS_PER_REPETITION,
    MAX_CAMPAIGN_USD,
    MAX_PER_RUN_USD,
    BatchEconomicsPlan,
    BatchEconomicsResult,
    BatchProviderEconomicsHarness,
    LiveBatchGate,
    ParentBatchObservation,
    PlannedBatchSubmission,
)
from .batch_provider_live_driver import (
    AdapterFactory,
    BatchProviderLiveBlocked,
    _contains_credential,
    _default_adapter_factory,
    _ServiceKeySource,
)
from .batch_provider_live_driver import (
    prepare as prepare_live_batch,
)
from .batch_provider_service_adapter import ARM_PHRASE as BATCH_ARM_PHRASE
from .campaign_http import provider_acknowledgement
from .evidence import AcceptanceCriterion, EvidenceStore

AUTHORIZATION_PHRASE = "AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V3"
PROFILE_ID = "evaluation-studio-v1-accelerated-demo-v3"
W2_GATE_ID = "accelerated-v3.workflow2.third-repetition"


class AcceleratedV3BlockedError(RuntimeError):
    """Stable fail-closed reason for V3 preflight or execution refusal."""


@dataclass(frozen=True, slots=True)
class V3Preflight:
    profile_sha256: str
    remediation_checksum_sha256: str
    prior_w2_checksum_sha256: str


def load_profile(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceleratedV3BlockedError("v3_profile_invalid") from exc
    if not isinstance(raw, dict):
        raise AcceleratedV3BlockedError("v3_profile_invalid")
    budgets = raw.get("budgets")
    gates = raw.get("gates")
    if (
        raw.get("profile_id") != PROFILE_ID
        or raw.get("status") != "proposed_unarmed"
        or raw.get("authorization_phrase") != AUTHORIZATION_PHRASE
        or not isinstance(budgets, dict)
        or budgets.get("maximum_new_live_runs") != 2
        or budgets.get("maximum_new_provider_calls") != 12
        or not isinstance(gates, list)
    ):
        raise AcceleratedV3BlockedError("v3_profile_invalid")
    w2 = next(
        (gate for gate in gates if isinstance(gate, dict) and gate.get("gate_id") == W2_GATE_ID),
        None,
    )
    if (
        not isinstance(w2, dict)
        or w2.get("maximum_new_live_runs") != 1
        or w2.get("maximum_provider_calls") != 8
    ):
        raise AcceleratedV3BlockedError("v3_profile_invalid")
    return raw


def verify_sealed_bundle(root: Path) -> str:
    """Verify a generic immutable evidence bundle and return its manifest digest."""
    try:
        root = root.expanduser().resolve(strict=True)
        checksum_path = root / "SHA256SUMS"
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AcceleratedV3BlockedError("remediation_bundle_invalid") from exc
    if root.is_symlink() or not root.is_dir() or not lines:
        raise AcceleratedV3BlockedError("remediation_bundle_invalid")
    expected = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != checksum_path
    }
    observed: set[str] = set()
    for line in lines:
        digest, separator, relative = line.partition("  ")
        candidate = Path(relative)
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in observed
        ):
            raise AcceleratedV3BlockedError("remediation_bundle_invalid")
        target = root / candidate
        if (
            not target.is_file()
            or target.is_symlink()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise AcceleratedV3BlockedError("remediation_bundle_invalid")
        observed.add(relative)
    if observed != expected:
        raise AcceleratedV3BlockedError("remediation_bundle_invalid")
    return hashlib.sha256(checksum_path.read_bytes()).hexdigest()


def preflight(
    *,
    profile_path: Path,
    authorization: str,
    remediation_bundle: Path,
    prior_w2_bundle: Path,
) -> V3Preflight:
    """Validate authorization before reading any execution prerequisite."""
    if authorization != AUTHORIZATION_PHRASE:
        raise AcceleratedV3BlockedError("v3_authorization_invalid")
    load_profile(profile_path)
    remediation_digest = verify_sealed_bundle(remediation_bundle)
    prior_w2_digest = verify_sealed_bundle(prior_w2_bundle)
    try:
        remediation_manifest = json.loads(
            (remediation_bundle / "manifest.json").read_text(encoding="utf-8")
        )
        prior_manifest = json.loads(
            (prior_w2_bundle / "manifest.json").read_text(encoding="utf-8")
        )
        prior_acceptance = json.loads(
            (prior_w2_bundle / "acceptance.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceleratedV3BlockedError("prior_w2_bundle_invalid") from exc
    if (
        not isinstance(remediation_manifest, dict)
        or remediation_manifest.get("checkpoint")
        != "post-stop-cost-rollup-remediation"
        or remediation_manifest.get("campaign_id") != "evaluation-studio-v1"
        or remediation_manifest.get("provider_calls_performed") != 0
        or remediation_manifest.get("run_submissions_performed") != 0
    ):
        raise AcceleratedV3BlockedError("remediation_bundle_invalid")
    parents = prior_manifest.get("parents") if isinstance(prior_manifest, dict) else None
    criteria = (
        prior_acceptance.get("criteria") if isinstance(prior_acceptance, dict) else None
    )
    status_by_id = {
        item.get("criterion_id"): item.get("status")
        for item in criteria or []
        if isinstance(item, dict)
    }
    if (
        prior_manifest.get("campaign_id") != "evaluation-studio-v1"
        or prior_manifest.get("historical_provider_calls_reconciled") != 16
        or not isinstance(parents, list)
        or {
            (item.get("repetition"), item.get("status"))
            for item in parents
            if isinstance(item, dict)
        }
        != {(1, "succeeded"), (2, "succeeded")}
        or status_by_id.get("workflow2.happy-1") != "pass"
        or status_by_id.get("workflow2.happy-2") != "pass"
    ):
        raise AcceleratedV3BlockedError("prior_w2_bundle_invalid")
    return V3Preflight(
        profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        remediation_checksum_sha256=remediation_digest,
        prior_w2_checksum_sha256=prior_w2_digest,
    )


def single_parent_plan(
    *, campaign_id: str, campaign_spend_before_usd: str | Decimal
) -> BatchEconomicsPlan:
    """Return the sole paid W2 submission permitted by the V3 profile."""
    try:
        spend = Decimal(str(campaign_spend_before_usd))
    except (InvalidOperation, ValueError) as exc:
        raise AcceleratedV3BlockedError("campaign_spend_invalid") from exc
    if not spend.is_finite() or spend < 0 or spend + MAX_PER_RUN_USD > MAX_CAMPAIGN_USD:
        raise AcceleratedV3BlockedError("campaign_capacity_insufficient")
    submission = PlannedBatchSubmission(
        campaign_id=campaign_id,
        repetition=3,
        items=ITEMS_PER_REPETITION,
        concurrency=CONCURRENCY,
        per_run_cap_usd=MAX_PER_RUN_USD,
        campaign_cap_usd=MAX_CAMPAIGN_USD,
    )
    return BatchEconomicsPlan(
        criterion_id=CRITERION_ID,
        campaign_id=campaign_id,
        repetitions=1,
        items_per_repetition=ITEMS_PER_REPETITION,
        concurrency=CONCURRENCY,
        per_run_cap_usd=MAX_PER_RUN_USD,
        campaign_cap_usd=MAX_CAMPAIGN_USD,
        campaign_spend_before_usd=spend,
        submissions=(submission,),
    )


def reconcile_single_parent(
    plan: BatchEconomicsPlan,
    parent: ParentBatchObservation,
) -> BatchEconomicsResult:
    if plan.repetitions != 1 or len(plan.submissions) != 1:
        raise AcceleratedV3BlockedError("v3_w2_plan_invalid")
    if plan.submissions[0].repetition != 3:
        raise AcceleratedV3BlockedError("v3_w2_plan_invalid")
    try:
        return BatchProviderEconomicsHarness._reconcile(plan, (parent,))
    except (RuntimeError, TypeError, ValueError) as exc:
        raise AcceleratedV3BlockedError("v3_w2_reconciliation_failed") from exc


def seal_result(
    *,
    result: BatchEconomicsResult,
    preflight: V3Preflight,
    destination: Path,
) -> Path:
    """Seal the new repetition and its immutable historical prerequisites."""
    if (
        not result.passed
        or result.plan.repetitions != 1
        or len(result.parent_observations) != 1
        or result.parent_observations[0].repetition != 3
    ):
        raise AcceleratedV3BlockedError("v3_w2_result_invalid")
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "bundle"
        store = EvidenceStore(staging)
        store.write_manifest(
            {
                "schema_version": 1,
                "checkpoint": W2_GATE_ID,
                "campaign_id": result.plan.campaign_id,
                "acceptance_profile_sha256": preflight.profile_sha256,
                "w1_remediation_checksum_sha256": (
                    preflight.remediation_checksum_sha256
                ),
                "prior_w2_checksum_sha256": preflight.prior_w2_checksum_sha256,
                "new_parent_runs": 1,
                "new_provider_calls_maximum": 8,
                "historical_parent_repetitions": [1, 2],
                "new_parent_repetition": 3,
                "credential_value_retained": False,
            }
        )
        store._write_exclusive(
            Path("reconciliation/workflow2-repetition-3.json"), result.as_dict()
        )
        evidence = ("reconciliation/workflow2-repetition-3.json",)
        store.finalize_bundle(
            acceptance=(
                AcceptanceCriterion("workflow2.happy-3", "pass", evidence),
                AcceptanceCriterion("workflow2.aggregate-economics", "pass", evidence),
            ),
            report_markdown=(
                "# Accelerated V3 Workflow 2\n\n"
                "The sole V3 parent is repetition three: eight isolated children at "
                "concurrency four with ordered indexes, signed chains, unique identities, "
                "complete reservations, and reconciled Audit/run/local/Economics totals. "
                "The manifest pins the checksum-sealed repetition-one/two evidence; this "
                "bundle does not rewrite either historical source.\n"
            ),
        )
        verify_sealed_bundle(staging)
        staging.replace(destination)
    return destination


def _campaign_spend(path: Path, *, campaign_id: str, tenant_id: str) -> Decimal:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                """SELECT status, actual_cost_usd, held_cost_usd FROM cost_reservations
                WHERE tenant_id = ? AND campaign_id = ?""",
                (tenant_id, campaign_id),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise AcceleratedV3BlockedError("campaign_spend_unavailable") from exc
    total = Decimal("0")
    for row in rows:
        try:
            if row["status"] == "committed":
                total += Decimal(str(row["actual_cost_usd"]))
            elif row["status"] in {"reserved", "ambiguous"}:
                total += Decimal(str(row["held_cost_usd"]))
            elif row["status"] != "released":
                raise ValueError
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AcceleratedV3BlockedError("campaign_spend_invalid") from exc
    return total


async def _submit_one(prepared, plan: BatchEconomicsPlan, adapter_factory: AdapterFactory):
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
    gate.validate()
    try:
        parent = await adapter.submit_parent(plan.submissions[0])
        service_key = auth_source()
        try:
            if _contains_credential(parent.as_dict(), service_key):
                raise AcceleratedV3BlockedError("observation_contains_service_credential")
        finally:
            service_key = ""
        return parent
    finally:
        collector = getattr(adapter, "_collector", None)
        audit_source = getattr(collector, "audit_source", None)
        close = getattr(audit_source, "close", None)
        if callable(close):
            close()


def execute(
    *,
    profile_path: Path,
    authorization: str,
    remediation_bundle: Path,
    prior_w2_bundle: Path,
    campaign_config: Path,
    readiness_attestation: Path,
    wiring_config: Path,
    service_api_key_file: Path,
    destination: Path,
    environment: Mapping[str, str],
    adapter_factory: AdapterFactory = _default_adapter_factory,
) -> Path:
    """Execute and seal exactly one paid parent after every V3 interlock passes."""
    checked = preflight(
        profile_path=profile_path,
        authorization=authorization,
        remediation_bundle=remediation_bundle,
        prior_w2_bundle=prior_w2_bundle,
    )
    try:
        prepared = prepare_live_batch(
            campaign_config=campaign_config,
            readiness_attestation=readiness_attestation,
            wiring_config=wiring_config,
            service_api_key_file=service_api_key_file,
            output=destination,
            arm=BATCH_ARM_PHRASE,
            environment=environment,
        )
        spend = _campaign_spend(
            prepared.wiring.econ_database,
            campaign_id=prepared.campaign.campaign_id,
            tenant_id=prepared.campaign.tenant_id,
        )
        plan = single_parent_plan(
            campaign_id=prepared.campaign.campaign_id,
            campaign_spend_before_usd=spend,
        )
        parent = asyncio.run(_submit_one(prepared, plan, adapter_factory))
        result = reconcile_single_parent(plan, parent)
        return seal_result(result=result, preflight=checked, destination=destination)
    except (AcceleratedV3BlockedError, FileExistsError):
        raise
    except BatchProviderLiveBlocked as exc:
        raise AcceleratedV3BlockedError(exc.code) from exc
    except Exception as exc:
        raise AcceleratedV3BlockedError("v3_w2_execution_failed") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accelerated-v3-w2-driver")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--remediation-bundle", required=True, type=Path)
    parser.add_argument("--prior-w2-bundle", required=True, type=Path)
    parser.add_argument("--campaign-config", required=True, type=Path)
    parser.add_argument("--readiness-attestation", required=True, type=Path)
    parser.add_argument("--wiring-config", required=True, type=Path)
    parser.add_argument("--service-api-key-file", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = execute(
            profile_path=args.profile,
            authorization=args.authorization,
            remediation_bundle=args.remediation_bundle,
            prior_w2_bundle=args.prior_w2_bundle,
            campaign_config=args.campaign_config,
            readiness_attestation=args.readiness_attestation,
            wiring_config=args.wiring_config,
            service_api_key_file=args.service_api_key_file,
            destination=args.destination,
            environment=os.environ,
        )
    except (AcceleratedV3BlockedError, FileExistsError) as exc:
        reason = exc.args[0] if exc.args else "v3_w2_blocked"
        print(json.dumps({"status": "blocked", "reason": str(reason)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "verified",
                "destination": str(destination),
                "new_live_runs": 1,
                "maximum_provider_calls": 8,
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "AUTHORIZATION_PHRASE",
    "AcceleratedV3BlockedError",
    "V3Preflight",
    "build_parser",
    "execute",
    "load_profile",
    "preflight",
    "reconcile_single_parent",
    "seal_result",
    "single_parent_plan",
    "verify_sealed_bundle",
]


if __name__ == "__main__":
    raise SystemExit(main())
