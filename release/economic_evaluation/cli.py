"""Fail-closed, candidate-bound evidence for the frozen contract (partial implementation)."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTRACT = "1ccc8604b6632c39fecb6fcaed4dd6e1b4648f520bb677e46311be4f4dbe04da"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def acceptance_passed(gates):
    required = [gate for gate in gates if gate["required"]]
    return bool(required) and all(gate["status"] == "passed" for gate in required)


def identity():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT)

    diff = git("diff", "--binary", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    # Generated evidence is separately hashed below: excluding it from candidate
    # source identity avoids a bundle recursively identifying its own bytes.
    source_untracked = {
        path: sha((ROOT / path).read_bytes())
        for path in untracked
        if path and not path.startswith("release/economic_evaluation/evidence/")
    }
    return {
        "revision": git("rev-parse", "HEAD").decode().strip(),
        "worktree": str(ROOT),
        "tracked_diff_sha256": sha(diff),
        "tracked_diff": diff.decode(),
        "untracked_source_hashes": source_untracked,
        "untracked_source_identity_sha256": sha(
            json.dumps(source_untracked, sort_keys=True).encode()
        ),
        "status": git("status", "--short").decode(),
    }


def execute_tests(modules):
    command = [sys.executable, "-m", "unittest", *modules, "-v"]
    with tempfile.TemporaryDirectory(prefix="zeroth-eval-db-") as directory:
        environment = dict(
            os.environ,
            ECP_JWT_SECRET="test-econ-evaluation-not-a-real-key",
            ECP_DATABASE_URL=f"sqlite:///{Path(directory) / 'test.db'}",
        )
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=300
        )
    skipped = "skipped=" in result.stderr or "... skipped " in result.stderr
    return {
        "command": command,
        "exit_code": result.returncode,
        "status": "passed" if result.returncode == 0 and not skipped else "failed",
        "stdout": result.stdout,
        "stderr": result.stderr,
        "skips_detected": skipped,
    }


def build_bundle():
    from release.economic_evaluation import mutations, numerical

    started = datetime.now(UTC).isoformat()
    before = identity()
    contract_hash = sha((ROOT / "docs/operations/economic-evaluation-contract-v1.md").read_bytes())
    gates = [
        {
            "name": "contract_identity",
            "required": True,
            "status": "passed" if contract_hash == CONTRACT else "failed",
        }
    ]
    for name, modules in (
        (
            "focused_exact_properties",
            [
                "release.economic_evaluation.test_known_answers",
                "release.economic_evaluation.test_invariants",
                "release.economic_evaluation.test_numerical",
                "release.economic_evaluation.test_bundle",
                "release.economic_evaluation.test_forecast_defects",
                "release.economic_evaluation.test_named_boundary_faults",
                "release.economic_evaluation.test_predictive_harness",
            ],
        ),
        ("full_K01_K11_candidate_adapter", ["release.economic_evaluation.test_finite_adapters"]),
        (
            "monthly_horizon_and_durable_missingness",
            ["release.economic_evaluation.test_horizon_and_missingness"],
        ),
        ("public_experimental_cutoff", ["release.economic_evaluation.test_experimental_cutoff"]),
        (
            "aggregate_future_request_semantics",
            ["release.economic_evaluation.test_future_requests"],
        ),
        ("local_smtp_faults", ["release.economic_evaluation.test_delivery_boundaries"]),
    ):
        gates.append({"name": name, "required": True, **execute_tests(modules)})
    numerical_result = numerical.run()
    gates.append(
        {
            "name": "numerical_sampler",
            "required": True,
            "status": "passed" if numerical_result["passed"] else "failed",
            "raw_results": numerical_result,
        }
    )
    mutation_result = mutations.run()
    gates.append(
        {
            "name": "named_fault_injections",
            "required": True,
            "status": "passed" if mutation_result["passed"] else "failed",
            "raw_results": mutation_result,
        }
    )
    for name, status, reason in [
        (
            "full_output_fidelity",
            "untested",
            "Full engine/storage/API/PDF/MIME chain and visual QA are untested. "
            "Current MIME test checks transport bytes only.",
        ),
        (
            "predictive_causal_harness",
            "untested",
            "V3 descriptive runner is separate; this bundle does not execute or certify it. "
            "Full predictive/causal stress worlds remain required and unexecuted.",
        ),
        (
            "mc_threshold_uncertainty",
            "untested",
            "No safe-recommendation confidence-bound repair implemented.",
        ),
        (
            "production_predictive_validity",
            "blocked",
            "No approved production thresholds or customer evidence.",
        ),
        ("customer_data_sufficiency", "blocked", "No approved minimum or customer evidence."),
        ("whole_repository", "untested", "Focused suites do not certify the whole repository."),
    ]:
        gates.append({"name": name, "required": True, "status": status, "reason": reason})
    after = identity()
    stable = all(
        before[key] == after[key]
        for key in ("revision", "tracked_diff_sha256", "untracked_source_identity_sha256")
    )
    gates.append(
        {
            "name": "candidate_unchanged_during_run",
            "required": True,
            "status": "passed" if stable else "failed",
        }
    )
    hashes = {
        str(path.relative_to(ROOT)): sha(path.read_bytes())
        for path in [
            ROOT / "uv.lock",
            HERE / "known_answers_v1.json",
            HERE / "numerical_manifest_v1.json",
            HERE / "predictive_manifest_proposal_v1.json",
            HERE / "predictive_manifest_proposal_v2.md",
            HERE / "predictive_manifest_proposal_v3.md",
            HERE / "predictive_manifest_v3_supplement.md",
        ]
    }
    baseline_hashes = {
        path.name: sha(path.read_bytes()) for path in (HERE / "evidence").glob("baseline-*.txt")
    }
    passed = acceptance_passed(gates)
    return {
        "bundle_version": 1,
        "scope": "partial engineering candidate; not self-certification",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "candidate": before,
        "candidate_after": after,
        "contract_sha256": contract_hash,
        "hashes": hashes,
        "baseline_evidence_hashes": baseline_hashes,
        "reproduce": [
            "uv sync --frozen --group dev --extra regulus",
            "uv run --frozen python -m release.economic_evaluation.cli",
        ],
        "python": sys.version,
        "platform": sys.platform,
        "gates": gates,
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "rollback": "No deployment, migration, or routing change. Revert repairs with review; "
        "retain immutable evidence and baseline commits.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "evidence" / "candidate-bundle.json")
    args = parser.parse_args()
    # Fail rather than silently replace a prior identified run.
    if args.output.exists():
        parser.error("output already exists; choose a fresh evidence filename")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    args.output.write_text(json.dumps(bundle, indent=2) + "\n")
    print(
        json.dumps(
            {
                "bundle": str(args.output),
                "passed": bundle["passed"],
                "gates": [
                    {"name": row["name"], "status": row["status"]} for row in bundle["gates"]
                ],
            },
            indent=2,
        )
    )
    return bundle["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
