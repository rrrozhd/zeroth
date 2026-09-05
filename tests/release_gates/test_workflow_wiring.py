"""R5, R6, R7 — promotion depends on the gate, evidence is produced, PRs stay fast.

Every assertion here parses the workflow YAML and reasons over the job graph.
A substring search of the raw file would pass on a commented-out job or a
`needs:` that names a job which does not exist, which is exactly the class of
mistake this has to catch.
"""

from __future__ import annotations

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from .conftest import ROOT

WORKFLOWS = ROOT / ".github/workflows"
RELEASE_WORKFLOW = WORKFLOWS / "release-zeroth-core.yml"
PROMOTION_WORKFLOW = WORKFLOWS / "promote-zeroth-core.yml"
GATES_WORKFLOW = WORKFLOWS / "release-gates.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
EVIDENCE_WORKFLOWS = (RELEASE_WORKFLOW, GATES_WORKFLOW, PROMOTION_WORKFLOW)

#: Reviewed workflows with fast pull-request checks. SDK publishing jobs are
#: manual-only; their guards are checked separately below.
BASE_PULL_REQUEST_WORKFLOWS = frozenset(
    {
        "ci.yml",
        "docs.yml",
        "examples.yml",
        "langgraph-compatibility.yml",
        "verify-extras.yml",
        "release-zeroth-sdk.yml",
    }
)

#: Work that belongs to a release candidate and must never reach a PR.
RELEASE_ONLY_WORK = (
    "docker build",
    "actions/attest",
    "pypa/gh-action-pypi-publish",
    "anchore/sbom-action",
    "test.pypi.org",
)

PRIVILEGED_ACTION_REFS = {
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5"),
    "actions/download-artifact": ("d3f86a106a0bac45b974a628896c90dbdf5c8093", "v4"),
    "actions/upload-artifact": ("ea165f8d65b6e75b540449e92b4886f43607fa02", "v4"),
    "actions/attest": ("1e69f48acb82d1966a394da916b4c1698aa569d6", "v4"),
    "anchore/sbom-action": ("e22c389904149dbc22b58101806040fa8d37a610", "v0"),
    "pypa/gh-action-pypi-publish": (
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
        "release/v1",
    ),
}


def _load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    document["on"] = document.pop(True, document.get("on"))
    return document


def _jobs(path: Path) -> dict:
    return _load(path)["jobs"]


def _needs(job: dict) -> list[str]:
    declared = job.get("needs", [])
    return [declared] if isinstance(declared, str) else list(declared)


def _ancestors(jobs: dict, name: str) -> set[str]:
    """Return every job ``name`` transitively depends on."""
    seen: set[str] = set()
    frontier = list(_needs(jobs[name]))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(_needs(jobs[current]))
    return seen


def _steps(job: dict) -> list[dict]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _scripts(path: Path) -> dict[str, str]:
    """Return every job's concatenated shell, keyed by job name.

    Quotes are stripped: ``--kind "junit=x"`` and ``--kind junit=x`` pass the
    same argument, and the assertions here are about which arguments a step
    passes, not about how the shell was quoted.
    """
    return {
        name: "\n".join(step.get("run", "") for step in _steps(job))
        .replace('"', "")
        .replace("'", "")
        for name, job in _jobs(path).items()
    }


def test_privileged_release_jobs_pin_external_actions() -> None:
    found = 0
    for path in (RELEASE_WORKFLOW, PROMOTION_WORKFLOW):
        workflow = _load(path)
        source = path.read_text(encoding="utf-8")
        privileged_jobs = {
            name: job
            for name, job in workflow["jobs"].items()
            if "write" in (job.get("permissions", workflow["permissions"])).values()
        }
        found += len(privileged_jobs)
        for job_name, job in privileged_jobs.items():
            for step in _steps(job):
                uses = str(step.get("uses", ""))
                if not uses or uses.startswith("./"):
                    continue
                action, _, ref = uses.partition("@")
                assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                    f"{path.name}:{job_name}: {action} is not pinned to a 40-hex commit"
                )
                assert action in PRIVILEGED_ACTION_REFS, (
                    f"{path.name}:{job_name}: unreviewed action {action}"
                )
                expected_ref, version = PRIVILEGED_ACTION_REFS[action]
                assert ref == expected_ref
                assert re.search(
                    rf"^\s*uses:\s*{re.escape(action)}@{ref}\s+#\s+{re.escape(version)}\s*$",
                    source,
                    re.MULTILINE,
                ), f"{path.name}:{job_name}: {action} pin has no {version} version comment"
    assert found


# --------------------------------------------------------------------------
# R5 — promotion depends on evidence validation
# --------------------------------------------------------------------------


def test_testpypi_publication_depends_on_the_candidate_evidence_gate():
    jobs = _jobs(RELEASE_WORKFLOW)

    assert "evidence-gate" in _ancestors(jobs, "publish-testpypi")


def test_pypi_promotion_is_a_separate_manual_final_validation_workflow():
    release_jobs = _jobs(RELEASE_WORKFLOW)
    promotion = _load(PROMOTION_WORKFLOW)
    job = promotion["jobs"]["promote"]
    script = _scripts(PROMOTION_WORKFLOW)["promote"]

    assert "publish-pypi" not in release_jobs
    assert set(promotion["on"]) == {"workflow_dispatch"}
    assert job["environment"] == "pypi"
    assert "--phase final" in script
    assert "pypa/gh-action-pypi-publish" in str(job["steps"])


@pytest.mark.parametrize(
    ("promotion_job", "gate_job", "phase"),
    [
        ("publish-testpypi", "evidence-gate", "candidate"),
    ],
)
def test_the_gate_a_promotion_depends_on_really_runs_the_validator(promotion_job, gate_job, phase):
    """Depending on a job *named* like a gate proves nothing.

    Renaming the command, dropping the phase, or narrowing it with a trigger
    would leave the dependency-graph assertions green while letting gates fall
    out of promotion, so assert what the job actually executes.
    """
    jobs = _jobs(RELEASE_WORKFLOW)
    assert gate_job in _ancestors(jobs, promotion_job)

    script = _scripts(RELEASE_WORKFLOW)[gate_job]

    assert "release/gates/cli.py verdict" in script or "release/gates/cli.py validate" in script
    assert f"--phase {phase}" in script
    # A trigger filter would narrow the gate set; promotion must see them all.
    assert "--trigger" not in script


def test_the_manual_promotion_seals_the_evidence_it_validated():
    script = _scripts(PROMOTION_WORKFLOW)["promote"]
    attests = [
        step
        for step in _steps(_jobs(PROMOTION_WORKFLOW)["promote"])
        if str(step.get("uses", "")).startswith("actions/attest")
    ]

    assert "release/gates/cli.py seal" in script
    assert attests, "the sealed evidence manifest is never attested"
    assert "evidence-manifest.json" in str(attests[0]["with"]["subject-path"])


def test_candidate_assembly_runs_even_when_a_producer_failed():
    """Otherwise the verdict disappears exactly when it is most needed.

    ``if: always()`` alone is not enough: it keeps the job alive, but a
    download step that hard-fails on an artifact an upstream gate never
    uploaded would still skip the verdict.
    """
    jobs = _jobs(RELEASE_WORKFLOW)

    for name in ("evidence-gate", "assemble-promotion-candidate"):
        assert jobs[name].get("if") == "always()", f"{name} is skipped when a producer fails"
        downloads = [
            step
            for step in _steps(jobs[name])
            if str(step.get("uses", "")).startswith("actions/download-artifact")
        ]
        assert downloads, f"{name} downloads no evidence"
        for step in downloads:
            assert step.get("continue-on-error") is True, (
                f"{name}: '{step.get('name')}' aborts the job when a producer never uploaded"
            )


def test_a_step_never_reads_an_artifact_downloaded_by_a_later_step():
    """Ordering bugs here fail every release at the same line.

    A step that consumes the candidate identity must come after the step that
    downloads it -- otherwise the file simply is not there yet.
    """
    for path in (RELEASE_WORKFLOW, GATES_WORKFLOW):
        for job_name, job in _jobs(path).items():
            downloaded_at: int | None = None
            for index, step in enumerate(_steps(job)):
                if str(step.get("uses", "")).startswith("actions/download-artifact"):
                    target = str(step.get("with", {}).get("path", ""))
                    if "release/evidence" in target:
                        downloaded_at = index if downloaded_at is None else downloaded_at
                script = step.get("run", "")
                if "candidate-identity" in script and "cli.py identity" not in script:
                    assert downloaded_at is not None and downloaded_at < index, (
                        f"{path.name}:{job_name} step {index} "
                        f"('{step.get('name')}') reads the candidate identity "
                        "before any step downloads it"
                    )


def test_every_needs_edge_names_a_job_that_exists():
    for path in (RELEASE_WORKFLOW, GATES_WORKFLOW):
        jobs = _jobs(path)
        for name, job in jobs.items():
            for dependency in _needs(job):
                assert dependency in jobs, f"{path.name}: {name} needs missing job {dependency}"


def test_the_evidence_gate_depends_on_every_job_that_produces_a_record():
    """A gate that does not wait for a producer would validate a torn evidence set."""
    jobs = _jobs(RELEASE_WORKFLOW)
    ancestors = _ancestors(jobs, "assemble-promotion-candidate")

    for producer in ("source-gates", "container-evidence", "smoke-from-testpypi"):
        assert producer in ancestors, f"the final gate does not wait for {producer}"


def test_the_release_workflow_reuses_the_gate_workflow_rather_than_copying_it():
    jobs = _jobs(RELEASE_WORKFLOW)

    assert jobs["source-gates"]["uses"] == "./.github/workflows/release-gates.yml"
    assert "workflow_call" in _load(GATES_WORKFLOW)["on"]


def test_the_called_workflow_measures_the_dist_the_release_actually_publishes():
    """Two independent builds differ byte-for-byte, which would read as mismatched."""
    jobs = _jobs(RELEASE_WORKFLOW)

    assert jobs["source-gates"]["with"]["dist_artifact"] == "dist"
    assert "dist_artifact" in _load(GATES_WORKFLOW)["on"]["workflow_call"]["inputs"]


# --------------------------------------------------------------------------
# R6 — every applicable evidence kind is produced and uploaded
# --------------------------------------------------------------------------


def _record_script(gate_id: str) -> tuple[str, str]:
    """Return the (workflow, job) shell that emits ``gate_id``'s record."""
    matches = [
        (path.name, name, script)
        for path in EVIDENCE_WORKFLOWS
        for name, script in _scripts(path).items()
        if f"--gate {gate_id} " in script or script.rstrip().endswith(f"--gate {gate_id}")
    ]
    assert len(matches) == 1, f"{gate_id} is emitted by {len(matches)} jobs, expected exactly 1"
    workflow, job, script = matches[0]
    return f"{workflow}:{job}", script


def test_every_gate_in_the_manifest_has_exactly_one_producing_job(manifest):
    for gate in manifest["gates"]:
        _record_script(gate["id"])


def test_applicable_evidence_kinds_are_produced_and_uploaded(manifest):
    for gate in manifest["gates"]:
        where, script = _record_script(gate["id"])
        for kind in gate["kinds"]:
            declared = manifest["evidence_kinds"][kind]
            assert declared["applicable"], f"{gate['id']} produces non-applicable kind {kind}"
            assert f"--kind {kind}=" in script, f"{where} does not attach the {kind} record"


def test_every_applicable_ci_kind_is_produced_by_some_gate(manifest):
    from gates.manifest import applicable_kinds

    produced = {kind for gate in manifest["gates"] for kind in gate["kinds"]}

    for kind in applicable_kinds(manifest, "ci"):
        assert kind in produced, f"{kind} is applicable and CI-produced but no gate emits it"


def test_manual_evidence_has_no_ci_producer_and_blocks_until_supplied(manifest):
    from gates.manifest import applicable_kinds

    _, script = _record_script("promotion")

    for kind in applicable_kinds(manifest, "manual"):
        assert f"--kind {kind}=" in script
    promotion = _load(PROMOTION_WORKFLOW)
    assert set(promotion["on"]) == {"workflow_dispatch"}
    assert "PROMOTE_ZEROTH_CORE" in script
    assert "${{ github.actor }}" in PROMOTION_WORKFLOW.read_text(encoding="utf-8")
    assert "Candidate-digest:" in script


def _cited_kind_paths(script: str) -> list[str]:
    return re.findall(r"--kind [a-z-]+=(\S+)", script)


def _uploaded_paths(path: Path, job_name: str) -> list[str]:
    """Return the paths this job publishes in artifacts the evidence gate reads.

    Only ``gate-*`` artifacts count. The job may upload the same file in some
    other artifact -- ``container-evidence`` also publishes
    ``langgraph-container-evidence`` -- but the evidence gate downloads
    ``gate-*`` and nothing else, so a file that travels only in another
    artifact is still absent when the record is validated.
    """
    paths: list[str] = []
    for step in _steps(_jobs(path)[job_name]):
        if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
            continue
        if not str(step.get("with", {}).get("name", "")).startswith("gate-"):
            continue
        declared = step.get("with", {}).get("path", "")
        paths.extend(line.strip() for line in str(declared).splitlines() if line.strip())
    return paths


def _is_covered(cited: str, uploaded: list[str]) -> bool:
    return any(
        fnmatch(cited, pattern) or cited.startswith(pattern.rstrip("*")) for pattern in uploaded
    )


@pytest.mark.parametrize(
    "gate_id",
    [
        "source",
        "package",
        "langgraph",
        "untrusted-code",
        "load-recovery",
        "deployment-smoke",
        "remote-acceptance",
        "promotion",
    ],
)
def test_every_cited_evidence_file_reaches_the_validating_job(gate_id):
    """A record may only cite a file the gate can actually resolve later.

    The evidence gate checks the repository out and downloads the ``gate-*``
    artifacts. So a cited path has to be either a committed file, or one the
    emitting job uploads in its own gate artifact. Citing a file that is
    generated somewhere else and never travels with the record makes the
    validator report `partial` -- blocking every release -- and no amount of
    the gate working locally would reveal it.
    """
    where, script = _record_script(gate_id)
    workflow_name, job_name = where.split(":", 1)
    workflow = next(path for path in EVIDENCE_WORKFLOWS if path.name == workflow_name)
    uploaded = _uploaded_paths(workflow, job_name)

    cited = _cited_kind_paths(script)
    assert cited, f"{gate_id} cites no evidence file"

    for candidate_path in cited:
        committed = (ROOT / candidate_path).exists() and _tracked(candidate_path)
        # A gate emitted inside the validating job never travels as an
        # artifact: the file is already on that runner. It still has to be
        # created there rather than assumed to exist.
        created_here = candidate_path in script and (
            job_name.startswith("evidence-gate") or workflow == PROMOTION_WORKFLOW
        )
        assert committed or created_here or _is_covered(candidate_path, uploaded), (
            f"{where} cites {candidate_path}, which is neither committed, created in "
            f"this job, nor uploaded with the record (uploads: {uploaded})"
        )


def _tracked(relative: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def test_an_artifacts_paths_share_one_root(manifest):
    """upload-artifact roots the archive at the paths' common ancestor.

    Mixing directories silently moves that root, so the download lands a level
    deeper than the record's own paths expect.

    Only the ``gate-*`` artifacts are checked. They are the ones downloaded
    into ``release/evidence`` and then resolved by path, so their root has to
    match. ``langgraph-container-evidence`` deliberately mixes roots and is
    downloaded by nobody, so it is none of this test's business.
    """
    for path in EVIDENCE_WORKFLOWS:
        for job_name in _jobs(path):
            for step in _steps(_jobs(path)[job_name]):
                if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
                    continue
                if not str(step.get("with", {}).get("name", "")).startswith("gate-"):
                    continue
                declared = [
                    line.strip()
                    for line in str(step.get("with", {}).get("path", "")).splitlines()
                    if line.strip()
                ]
                parents = {str(Path(item).parent) for item in declared}
                assert len(parents) <= 1, (
                    f"{path.name}:{job_name} uploads paths from {sorted(parents)}; "
                    "the artifact root moves to their common ancestor"
                )


def test_each_gate_job_uploads_its_evidence(manifest):
    uploads = {
        f"{path.name}:{name}"
        for path in EVIDENCE_WORKFLOWS
        for name, job in _jobs(path).items()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    }

    for gate in manifest["gates"]:
        where, _ = _record_script(gate["id"])
        assert where in uploads, f"{where} emits {gate['id']} evidence but uploads nothing"


def test_no_two_jobs_upload_the_same_artifact_name():
    """The release run executes both workflows, so artifact names share one namespace."""
    names: dict[str, str] = {}
    for path in EVIDENCE_WORKFLOWS:
        for job_name, job in _jobs(path).items():
            for step in _steps(job):
                if not str(step.get("uses", "")).startswith("actions/upload-artifact"):
                    continue
                artifact = step.get("with", {}).get("name")
                if not artifact or "${{" in str(artifact):
                    continue
                where = f"{path.name}:{job_name}"
                assert artifact not in names, (
                    f"{where} and {names[artifact]} both upload {artifact!r}"
                )
                names[artifact] = where


def test_the_evidence_bundle_is_retained(manifest):
    retained = [
        step
        for path in (RELEASE_WORKFLOW, GATES_WORKFLOW)
        for job in _jobs(path).values()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
        and "retention-days" in step.get("with", {})
    ]

    assert retained, "no evidence bundle is retained"
    assert all(int(step["with"]["retention-days"]) >= 30 for step in retained)


# --------------------------------------------------------------------------
# R7 — fast PR checks are preserved
# --------------------------------------------------------------------------


def _pull_request_workflows() -> list[Path]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        # `on:` is a mapping when triggers carry filters and a list when they
        # do not; membership reads the same either way.
        triggers = _load(path)["on"]
        if isinstance(triggers, (dict, list)) and "pull_request" in triggers:
            found.append(path)
    return found


def test_pull_request_checks_stay_fast():
    """The complete matrix belongs to release candidates, not to every push."""
    for path in _pull_request_workflows():
        for job in _jobs(path).values():
            # Recognize only this strict manual-dispatch guard. Unknown or
            # broader conditions remain subject to the PR-cost restriction.
            if re.fullmatch(
                r"github\.event_name == 'workflow_dispatch' && inputs\.target == '(testpypi|pypi)'",
                str(job.get("if", "")),
            ):
                continue
            haystack = "\n".join(
                str(step.get("run", "")) + "\n" + str(step.get("uses", "")) for step in _steps(job)
            )
            for expensive in RELEASE_ONLY_WORK:
                assert expensive not in haystack, f"{path.name} runs {expensive} on pull requests"


def test_the_gate_matrix_never_runs_on_pull_requests():
    triggers = _load(GATES_WORKFLOW)["on"]

    assert "pull_request" not in triggers
    assert set(triggers) == {"schedule", "workflow_dispatch", "workflow_call"}


def test_the_set_of_pull_request_workflows_is_unchanged():
    assert {path.name for path in _pull_request_workflows()} == BASE_PULL_REQUEST_WORKFLOWS


def test_pull_requests_run_only_the_portable_pr_critical_security_tier():
    jobs = _jobs(CI_WORKFLOW)
    job = jobs["security-regression"]
    script = "\n".join(step.get("run", "") for step in _steps(job))

    assert job.get("if") == "github.event_name == 'pull_request'"
    assert "python -m release.security.pytest_gate" in script
    assert "--tier pr-critical" in script
    assert "--tier release-candidate" not in script
    assert "--results release/evidence/security-pr-outcomes.json" in script
    assert "--junitxml release/evidence/security-pr-junit.xml" in script
    assert "--pytest-arg=-q" in script
    assert "docker" not in script.lower()
    assert not job.get("services")


def test_release_candidate_security_job_has_healthy_redis_and_docker_postgres_access():
    job = _jobs(GATES_WORKFLOW)["security-regression"]
    redis = job["services"]["redis"]

    assert redis["image"] == "redis:7.4-alpine"
    assert "redis-cli ping" in redis["options"]
    assert job["env"]["ZEROTH_TEST_REDIS_URL"] == "redis://localhost:6379/15"
    assert job["runs-on"] == "ubuntu-latest", "the RC needs the hosted Docker daemon"


def test_release_candidate_security_job_runs_all_checks_then_always_records_and_uploads():
    jobs = _jobs(GATES_WORKFLOW)
    job = jobs["security-regression"]
    run_step = next(step for step in _steps(job) if "SECURITY_GATE_RC" in step.get("run", ""))
    script = run_step["run"]

    expected = [
        "python -m release.security.pytest_gate",
        "--tier release-candidate",
        "--results release/evidence/security-rc-outcomes.json",
        "--junitxml release/evidence/security-rc-junit.xml",
        "matrix verify-coverage",
        "--output release/evidence/security-coverage.json",
        "matrix verify-outcomes",
        "--output release/evidence/security-outcome-verdict.json",
        "python -m release.security.scan release/evidence",
        "--output release/evidence/security-scan.json",
    ]
    for token in expected:
        assert token in script
    assert "set +e" in script
    assert "set -e" not in script.replace("set +e", "")
    assert script.index("release.security.scan") < script.index("cli.py record")
    for code in ("SECURITY_GATE_RC", "COVERAGE_RC", "OUTCOME_RC", "SCAN_RC"):
        assert f"{code}=$?" in script
    for result in (
        "security-matrix=$(status ${SECURITY_GATE_RC})",
        "coverage-complete=$(status ${COVERAGE_RC})",
        "distributed-no-skips=$(status ${OUTCOME_RC})",
        "credential-scan=$(status ${SCAN_RC})",
    ):
        assert f'--result "{result}"' in script
    assert "--kind junit=release/evidence/security-rc-junit.xml" in script
    assert "--kind security=release/evidence/security-scan.json" in script
    assert "--output release/evidence/security-regression.json" in script

    uploads = [
        step
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert len(uploads) == 1
    assert uploads[0].get("if") == "always()"
    paths = str(uploads[0]["with"]["path"])
    for evidence in (
        "security-rc-junit.xml",
        "security-coverage.json",
        "security-outcome-verdict.json",
        "security-scan.json",
        "security-regression.json",
    ):
        assert evidence in paths
    assert "security-regression" in _needs(jobs["evidence-gate"])


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_workflow_parses_and_declares_jobs(path: Path):
    document = _load(path)

    assert document["on"], f"{path.name} declares no trigger"
    assert document["jobs"], f"{path.name} declares no job"


def test_load_receipt_has_git_before_checkout() -> None:
    """A slim runner must produce a Git checkout for exact-source load receipts."""
    steps = _steps(_jobs(GATES_WORKFLOW)["load-recovery"])
    checkout = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    preparation = "\n".join(str(step.get("run", "")) for step in steps[:checkout])
    assert re.search(r"apt-get\s+install[^\n]*\bgit\b", preparation), (
        "install Git before checkout; the REST fallback cannot supply receipt Git metadata"
    )
    assert "git --version" in preparation, "fail before checkout if Git provisioning failed"


@pytest.mark.parametrize("job", ["source", "package"])
def test_full_suite_gate_fetches_baseline_source_history(job):
    checkout = next(step for step in _steps(_jobs(GATES_WORKFLOW)[job])
                    if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout.get("with", {}).get("fetch-depth") == 0


@pytest.mark.parametrize("gate", ["source", "package", "langgraph", "untrusted-code"])
def test_gate_job_validates_its_record_after_writing_it(gate):
    _, script = _record_script(gate)
    validation = f"python release/gates/cli.py validate --gate {gate}"
    assert validation in script
    assert script.index(validation) > script.index(f"cat release/evidence/{gate}.json")


def test_load_receipt_verifies_checkout_in_the_regular_step_environment() -> None:
    steps = _steps(_jobs(GATES_WORKFLOW)["load-recovery"])
    checkout = next(i for i, step in enumerate(steps)
                    if str(step.get("uses", "")).startswith("actions/checkout@"))
    verify = next((i for i, step in enumerate(steps)
                   if step.get("name") == "Verify source receipt checkout"), None)
    assert verify is not None, "verify Git outside checkout's temporary HOME"
    assert verify == checkout + 1
    command = steps[verify]["run"]
    assert 'git config --global --add safe.directory "$GITHUB_WORKSPACE"' in command
    assert 'git -C "$GITHUB_WORKSPACE" rev-parse HEAD' in command
    assert '"$GITHUB_SHA"' in command
    assert "set -euo pipefail" in command
    assert "*" not in command, "do not trust unrelated repositories"


@pytest.mark.parametrize("guard", ["", "always()", "github.event_name == 'pull_request'"])
def test_pr_cost_check_rejects_sdk_publish_guard_regression(monkeypatch, guard) -> None:
    """Removing or broadening a publisher guard must make the PR-cost audit fail."""
    original = _jobs

    def mutated(path: Path) -> dict:
        jobs = original(path)
        if path.name == "release-zeroth-sdk.yml":
            jobs["publish-pypi"]["if"] = guard
        return jobs

    monkeypatch.setitem(globals(), "_jobs", mutated)
    with pytest.raises(AssertionError, match="release-zeroth-sdk.yml runs"):
        test_pull_request_checks_stay_fast()


def test_untrusted_gate_executes_mcp_and_records_failure(tmp_path):
    """Exercise the shell so an omitted/skipped MCP case cannot look green."""
    import os

    script = "\n".join(
        step.get("run", "") for step in _steps(_jobs(GATES_WORKFLOW)["untrusted-code"])
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls"
    stubs = {
        "docker": '#!/bin/bash\nif [[ "$1" == build ]]; then echo "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" > "$MCP_IID_PATH"; fi\n',
        "uv": '#!/bin/bash\necho "uv $* image=${ZEROTH_TEST_MCP_IMAGE:-missing}" >> "$CALL_LOG"\nif [[ "$*" == *test_mcp_docker_transport.py* ]]; then [[ "${ZEROTH_TEST_MCP_IMAGE:-}" == sha256:* ]] || exit 99; exit 42; fi\n',
        "python": '#!/bin/bash\necho "python $*" >> "$CALL_LOG"\n',
    }
    for name, body in stubs.items():
        executable = commands / name
        executable.write_text(body)
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "MCP_IID_PATH": str(tmp_path / "release/evidence/untrusted-mcp-image.txt"),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    calls = log.read_text()
    assert "test_mcp_docker_transport.py" in calls, (result, calls)
    assert "image=sha256:" in calls
    assert "sandbox-hardening=failed" in calls


def test_current_compatibility_snapshot_is_shared_and_verified():
    jobs = _jobs(GATES_WORKFLOW)
    candidate_script = "\n".join(step.get("run", "") for step in _steps(jobs["candidate"]))
    langgraph_script = "\n".join(step.get("run", "") for step in _steps(jobs["langgraph"]))
    snapshot = "release/evidence/langgraph-compatibility.json"
    assert "candidate_compatibility snapshot" in candidate_script
    assert (
        "--group gateway-conformance --extra langgraph --extra langgraph-gateway"
        in candidate_script
    )
    assert (
        "uv sync --frozen --group gateway-conformance --extra langgraph --extra langgraph-gateway"
        in langgraph_script
    )
    assert f"--compatibility {snapshot}" in candidate_script
    upload = next(
        step
        for step in _steps(jobs["candidate"])
        if step.get("name") == "Upload candidate identity"
    )
    assert snapshot in upload["with"]["path"]
    assert any(
        step.get("with", {}).get("name") == "candidate-identity"
        and "download-artifact" in step.get("uses", "")
        for step in _steps(jobs["langgraph"])
    )
    assert "candidate_compatibility verify" in langgraph_script
    assert "candidate_compatibility results" in langgraph_script
    assert f"--snapshot {snapshot}" in langgraph_script
    assert "--identity release/evidence/candidate-identity.json" in langgraph_script
    assert "cp release/langgraph/compatibility.json" not in langgraph_script
    assert "--manifest release/langgraph/release-manifest.json" not in langgraph_script
    assert langgraph_script.index("candidate_compatibility verify") < langgraph_script.index(
        "-m langgraph_conformance"
    )
    assert langgraph_script.index("candidate_compatibility results") > langgraph_script.index(
        "harness.py benchmark"
    )
