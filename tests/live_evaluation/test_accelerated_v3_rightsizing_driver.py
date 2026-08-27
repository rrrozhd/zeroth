from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import release.live_evaluation.accelerated_v3_rightsizing_driver as subject
from release.live_evaluation.accelerated_v3_w2_driver import AUTHORIZATION_PHRASE


PROFILE = Path("release/live_evaluation/accelerated-acceptance-v3.json")


def _sealed_w2(root: Path, *, happy_status: str = "pass") -> Path:
    root.mkdir()
    values = {
        "manifest.json": {
            "checkpoint": "accelerated-v3.workflow2.third-repetition",
            "campaign_id": "evaluation-studio-v1",
            "new_parent_runs": 1,
            "new_provider_calls_maximum": 8,
        },
        "acceptance.json": {
            "criteria": [
                {"criterion_id": "workflow2.happy-3", "status": happy_status},
                {"criterion_id": "workflow2.aggregate-economics", "status": happy_status},
            ]
        },
    }
    rows = []
    for name, value in values.items():
        payload = (json.dumps(value, sort_keys=True) + "\n").encode()
        (root / name).write_bytes(payload)
        rows.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(sorted(rows)) + "\n")
    return root


def test_builds_exact_one_case_flagged_contract() -> None:
    contract = subject.build_contract(
        profile_path=PROFILE,
        cases_sha256="0" * 64,
    )

    assert contract.node_id == "analyze"
    assert contract.max_cases == 1
    assert contract.min_cases == 5
    assert contract.expected_provider_calls == 4
    assert contract.required_verdict == "flagged"


def test_preflight_requires_v3_authorization_and_passing_w2(tmp_path: Path) -> None:
    passing = _sealed_w2(tmp_path / "passing")
    subject.preflight(
        profile_path=PROFILE,
        authorization=AUTHORIZATION_PHRASE,
        w2_result_bundle=passing,
    )

    with pytest.raises(subject.AcceleratedV3RightsizingBlockedError, match="v3_authorization_invalid"):
        subject.preflight(
            profile_path=PROFILE,
            authorization="wrong",
            w2_result_bundle=tmp_path / "missing",
        )

    failing = _sealed_w2(tmp_path / "failing", happy_status="fail")
    with pytest.raises(subject.AcceleratedV3RightsizingBlockedError, match="v3_w2_gate_incomplete"):
        subject.preflight(
            profile_path=PROFILE,
            authorization=AUTHORIZATION_PHRASE,
            w2_result_bundle=failing,
        )


def test_execute_seals_flagged_four_call_plumbing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "actions").mkdir()
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "evaluation-studio-v1",
                "tenant_id": "evaluation-studio-v1",
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "embedding_model": "openai/text-embedding-3-small",
                "vector_backend": "chroma",
                "campaign_budget_usd": "10.00",
                "per_run_cap_usd": "0.25",
                "provider_secret_ref": "llm.openai",
                "artifact_root": str(artifact_root),
                "action_sink_root": str(artifact_root / "actions"),
            }
        )
    )
    wiring = SimpleNamespace(rightsizing_cases_sha256="0" * 64)
    wiring_path = tmp_path / "wiring.json"
    wiring_path.write_text("{}")
    monkeypatch.setattr(subject, "preflight", lambda **_values: "a" * 64)
    monkeypatch.setattr(subject, "_parse_wiring", lambda _value: wiring)
    invoked: list[object] = []

    def fake_execute(**values):
        invoked.append(values["contract"])
        values["output"].write_text(
            json.dumps(
                {
                    "status": "verified",
                    "campaign_id": "evaluation-studio-v1",
                    "experiment": {
                        "node_id": "analyze",
                        "cases": 1,
                        "min_cases": 5,
                        "verdict": "flagged",
                        "calls": [{"model": f"model-{index}"} for index in range(4)],
                    },
                    "economics": {
                        "provider_window_policy": "unavailable_campaign_local_only"
                    },
                }
            )
        )
        return values["output"]

    monkeypatch.setattr(subject, "execute_rightsizing", fake_execute)
    destination = tmp_path / "sealed-rightsizing"
    result = subject.execute(
        profile_path=PROFILE,
        authorization=AUTHORIZATION_PHRASE,
        w2_result_bundle=tmp_path / "w2",
        campaign_config=campaign_path,
        wiring_config=wiring_path,
        service_api_key_file=tmp_path / "service.key",
        destination=destination,
        environment={},
    )

    assert result == destination
    assert invoked[0].expected_provider_calls == 4
    assert invoked[0].required_verdict == "flagged"
    assert (destination / "SHA256SUMS").is_file()
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"][0]["criterion_id"] == (
        "accelerated-v3.rightsizing.one-case-plumbing"
    )
    assert acceptance["criteria"][0]["status"] == "pass"
