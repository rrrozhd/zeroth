"""Fail-closed one-case Rightsizing boundary for accelerated demo V3."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .accelerated_v3_w2_driver import (
    AUTHORIZATION_PHRASE,
    W2_GATE_ID,
    load_profile,
    verify_sealed_bundle,
)
from .config import CampaignConfig
from .evidence import AcceptanceCriterion, EvidenceStore
from .live_provider_gate import _object, _parse_wiring
from .rightsizing_live_checkpoint import ARM_PHRASE as RIGHTSIZING_ARM_PHRASE
from .rightsizing_live_driver import (
    RightsizingDriverBlockedError,
    RightsizingExecutionContract,
)
from .rightsizing_live_driver import (
    execute as execute_rightsizing,
)

RIGHTSIZING_GATE_ID = "accelerated-v3.rightsizing.one-case-plumbing"


class AcceleratedV3RightsizingBlockedError(RuntimeError):
    """Stable fail-closed reason for the V3 Rightsizing boundary."""


def build_contract(
    *, profile_path: Path, cases_sha256: str
) -> RightsizingExecutionContract:
    profile = load_profile(profile_path)
    gates = profile.get("gates")
    gate = next(
        (
            item
            for item in gates or []
            if isinstance(item, dict) and item.get("gate_id") == RIGHTSIZING_GATE_ID
        ),
        None,
    )
    if (
        not isinstance(gate, dict)
        or gate.get("maximum_new_live_runs") != 1
        or gate.get("maximum_provider_calls") != 4
        or gate.get("maximum_candidates") != 1
        or gate.get("maximum_cases") != 1
        or gate.get("minimum_cases_for_confirmation") != 5
    ):
        raise AcceleratedV3RightsizingBlockedError("v3_rightsizing_profile_invalid")
    return RightsizingExecutionContract(
        node_id="analyze",
        cases_sha256=cases_sha256,
        max_cases=1,
        min_cases=5,
        expected_provider_calls=4,
        required_verdict="flagged",
    )


def preflight(
    *, profile_path: Path, authorization: str, w2_result_bundle: Path
) -> str:
    if authorization != AUTHORIZATION_PHRASE:
        raise AcceleratedV3RightsizingBlockedError("v3_authorization_invalid")
    load_profile(profile_path)
    try:
        checksum = verify_sealed_bundle(w2_result_bundle)
        manifest = json.loads(
            (w2_result_bundle / "manifest.json").read_text(encoding="utf-8")
        )
        acceptance = json.loads(
            (w2_result_bundle / "acceptance.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise AcceleratedV3RightsizingBlockedError("v3_w2_gate_incomplete") from exc
    criteria = acceptance.get("criteria") if isinstance(acceptance, dict) else None
    status = {
        item.get("criterion_id"): item.get("status")
        for item in criteria or []
        if isinstance(item, dict)
    }
    if (
        not isinstance(manifest, dict)
        or manifest.get("checkpoint") != W2_GATE_ID
        or manifest.get("campaign_id") != "evaluation-studio-v1"
        or manifest.get("new_parent_runs") != 1
        or manifest.get("new_provider_calls_maximum") != 8
        or status.get("workflow2.happy-3") != "pass"
        or status.get("workflow2.aggregate-economics") != "pass"
    ):
        raise AcceleratedV3RightsizingBlockedError("v3_w2_gate_incomplete")
    return checksum


def execute(
    *,
    profile_path: Path,
    authorization: str,
    w2_result_bundle: Path,
    campaign_config: Path,
    wiring_config: Path,
    service_api_key_file: Path,
    destination: Path,
    environment: Mapping[str, str],
    adapter=None,
) -> Path:
    """Run one four-call experiment and seal only its bounded plumbing claim."""
    w2_checksum = preflight(
        profile_path=profile_path,
        authorization=authorization,
        w2_result_bundle=w2_result_bundle,
    )
    try:
        campaign = CampaignConfig.model_validate(
            _object(campaign_config, "campaign_configuration_invalid")
        )
        wiring = _parse_wiring(_object(wiring_config, "wiring_configuration_invalid"))
        contract = build_contract(
            profile_path=profile_path,
            cases_sha256=wiring.rightsizing_cases_sha256,
        )
    except Exception as exc:
        raise AcceleratedV3RightsizingBlockedError(
            "v3_rightsizing_configuration_invalid"
        ) from exc
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.", dir=destination.parent
        ) as temporary:
            temporary_root = Path(temporary)
            observation_path = temporary_root / "rightsizing.json"
            execute_rightsizing(
                campaign=campaign,
                wiring=wiring,
                service_api_key_file=service_api_key_file,
                output=observation_path,
                arm=RIGHTSIZING_ARM_PHRASE,
                environment=environment,
                adapter=adapter,
                contract=contract,
            )
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            if (
                observation.get("status") != "verified"
                or observation.get("experiment", {}).get("verdict") != "flagged"
                or len(observation.get("experiment", {}).get("calls", [])) != 4
                or observation.get("economics", {}).get("provider_window_policy")
                != "unavailable_campaign_local_only"
            ):
                raise AcceleratedV3RightsizingBlockedError(
                    "v3_rightsizing_observation_invalid"
                )
            staging = temporary_root / "bundle"
            store = EvidenceStore(staging)
            store.write_manifest(
                {
                    "schema_version": 1,
                    "checkpoint": RIGHTSIZING_GATE_ID,
                    "campaign_id": campaign.campaign_id,
                    "w2_result_checksum_sha256": w2_checksum,
                    "new_live_runs": 1,
                    "new_provider_calls": 4,
                    "maximum_cases": 1,
                    "minimum_cases_for_confirmation": 5,
                    "required_verdict": "flagged",
                    "shared_provider_window_available": False,
                    "credential_value_retained": False,
                }
            )
            store._write_exclusive(Path("reconciliation/rightsizing.json"), observation)
            store.finalize_bundle(
                acceptance=(
                    AcceptanceCriterion(
                        RIGHTSIZING_GATE_ID,
                        "pass",
                        ("reconciliation/rightsizing.json",),
                    ),
                ),
                report_markdown=(
                    "# Accelerated V3 Rightsizing\n\n"
                    "One measured case completed through the public endpoint with exactly "
                    "four provider calls. Audit, reservation, local Economics, and Regulus "
                    "identities reconcile. Because one case is below min_cases=5, the "
                    "required verdict is flagged, not confirmed. Provider project-window "
                    "usage remains permission-blocked; this passes plumbing only and does "
                    "not satisfy the shared-window or model-quality criteria.\n"
                ),
            )
            verify_sealed_bundle(staging)
            staging.replace(destination)
    except (AcceleratedV3RightsizingBlockedError, FileExistsError):
        raise
    except RightsizingDriverBlockedError as exc:
        raise AcceleratedV3RightsizingBlockedError(exc.code) from exc
    except Exception as exc:
        raise AcceleratedV3RightsizingBlockedError(
            "v3_rightsizing_execution_failed"
        ) from exc
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accelerated-v3-rightsizing-driver")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--w2-result-bundle", required=True, type=Path)
    parser.add_argument("--campaign-config", required=True, type=Path)
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
            w2_result_bundle=args.w2_result_bundle,
            campaign_config=args.campaign_config,
            wiring_config=args.wiring_config,
            service_api_key_file=args.service_api_key_file,
            destination=args.destination,
            environment=os.environ,
        )
    except (AcceleratedV3RightsizingBlockedError, FileExistsError) as exc:
        reason = exc.args[0] if exc.args else "v3_rightsizing_blocked"
        print(json.dumps({"status": "blocked", "reason": str(reason)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "verified",
                "destination": str(destination),
                "new_live_runs": 1,
                "provider_calls": 4,
                "verdict": "flagged",
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "AcceleratedV3RightsizingBlockedError",
    "RIGHTSIZING_GATE_ID",
    "build_contract",
    "build_parser",
    "execute",
    "main",
    "preflight",
]


if __name__ == "__main__":
    raise SystemExit(main())
