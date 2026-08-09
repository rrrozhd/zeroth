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
GATES_WORKFLOW = WORKFLOWS / "release-gates.yml"

#: Jobs that make the candidate available to someone else.
PROMOTION_JOBS = ("publish-testpypi", "publish-pypi")

#: Workflows that ran on pull requests before this change. Fast PR checks are
#: preserved by keeping this set exactly as it was.
BASE_PULL_REQUEST_WORKFLOWS = frozenset(
    {"ci.yml", "docs.yml", "examples.yml", "langgraph-compatibility.yml", "verify-extras.yml"}
)

#: Work that belongs to a release candidate and must never reach a PR.
RELEASE_ONLY_WORK = (
    "docker build",
    "actions/attest",
    "pypa/gh-action-pypi-publish",
    "anchore/sbom-action",
    "test.pypi.org",
)


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


# --------------------------------------------------------------------------
# R5 — promotion depends on evidence validation
# --------------------------------------------------------------------------


def test_promotion_jobs_depend_on_the_evidence_gate():
    jobs = _jobs(RELEASE_WORKFLOW)

    for name in PROMOTION_JOBS:
        assert name in jobs, f"{name} is not a job in the release workflow"
        ancestors = _ancestors(jobs, name)
        gates = {job for job in ancestors if job.startswith("evidence-gate")}
        assert gates, f"{name} can run without any evidence gate: depends on {sorted(ancestors)}"


def test_pypi_promotion_depends_on_the_final_gate_not_only_the_candidate_one():
    jobs = _jobs(RELEASE_WORKFLOW)

    assert "evidence-gate-final" in _ancestors(jobs, "publish-pypi")
    # TestPyPI is reached earlier, so it is gated on the candidate phase.
    assert "evidence-gate" in _ancestors(jobs, "publish-testpypi")


@pytest.mark.parametrize(
    ("promotion_job", "gate_job", "phase"),
    [
        ("publish-testpypi", "evidence-gate", "candidate"),
        ("publish-pypi", "evidence-gate-final", "final"),
    ],
)
def test_the_gate_a_promotion_depends_on_really_runs_the_validator(
    promotion_job, gate_job, phase
):
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


def test_the_final_gate_seals_the_evidence_it_validated():
    script = _scripts(RELEASE_WORKFLOW)["evidence-gate-final"]
    attests = [
        step
        for step in _steps(_jobs(RELEASE_WORKFLOW)["evidence-gate-final"])
        if str(step.get("uses", "")).startswith("actions/attest")
    ]

    assert "release/gates/cli.py seal" in script
    assert attests, "the sealed evidence manifest is never attested"
    assert "evidence-manifest.json" in str(attests[0]["with"]["subject-path"])


def test_the_gate_jobs_publish_a_verdict_even_when_a_producer_failed():
    """Otherwise the verdict disappears exactly when it is most needed."""
    jobs = _jobs(RELEASE_WORKFLOW)

    for name in ("evidence-gate", "evidence-gate-final"):
        assert jobs[name].get("if") == "always()", f"{name} is skipped when a producer fails"


def test_every_needs_edge_names_a_job_that_exists():
    for path in (RELEASE_WORKFLOW, GATES_WORKFLOW):
        jobs = _jobs(path)
        for name, job in jobs.items():
            for dependency in _needs(job):
                assert dependency in jobs, f"{path.name}: {name} needs missing job {dependency}"


def test_the_evidence_gate_depends_on_every_job_that_produces_a_record():
    """A gate that does not wait for a producer would validate a torn evidence set."""
    jobs = _jobs(RELEASE_WORKFLOW)
    ancestors = _ancestors(jobs, "evidence-gate-final")

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
        for path in (RELEASE_WORKFLOW, GATES_WORKFLOW)
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
    # The signoff is read from the repository, never synthesised as passed.
    assert "release/signoff/" in script
    assert "SIGNOFF_STATUS=failed" in script


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
        fnmatch(cited, pattern) or cited.startswith(pattern.rstrip("*"))
        for pattern in uploaded
    )


@pytest.mark.parametrize(
    "gate_id",
    [
        "source",
        "package",
        "langgraph",
        "untrusted-code",
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
    workflow = RELEASE_WORKFLOW if workflow_name == RELEASE_WORKFLOW.name else GATES_WORKFLOW
    uploaded = _uploaded_paths(workflow, job_name)

    cited = _cited_kind_paths(script)
    assert cited, f"{gate_id} cites no evidence file"

    for candidate_path in cited:
        if "$" in candidate_path:
            # A shell variable: the operator-supplied signoff, resolved from
            # the checkout at release time. Assert the script derives it from
            # the documented location rather than from anywhere it likes.
            name = candidate_path.strip("${}")
            assert re.search(rf"{name}=release/signoff/", script), (
                f"{where} cites ${{{name}}}, which is not derived from release/signoff/"
            )
            continue
        committed = (ROOT / candidate_path).exists() and _tracked(candidate_path)
        # A gate emitted inside the validating job never travels as an
        # artifact: the file is already on that runner. It still has to be
        # created there rather than assumed to exist.
        created_here = job_name.startswith("evidence-gate") and candidate_path in script
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
    for path in (RELEASE_WORKFLOW, GATES_WORKFLOW):
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
        for path in (RELEASE_WORKFLOW, GATES_WORKFLOW)
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
    for path in (RELEASE_WORKFLOW, GATES_WORKFLOW):
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
        script = "\n".join(_scripts(path).values())
        uses = " ".join(
            str(step.get("uses", ""))
            for job in _jobs(path).values()
            for step in _steps(job)
        )
        haystack = f"{script}\n{uses}"
        for expensive in RELEASE_ONLY_WORK:
            assert expensive not in haystack, f"{path.name} runs {expensive} on pull requests"


def test_the_gate_matrix_never_runs_on_pull_requests():
    triggers = _load(GATES_WORKFLOW)["on"]

    assert "pull_request" not in triggers
    assert set(triggers) == {"schedule", "workflow_dispatch", "workflow_call"}


def test_the_set_of_pull_request_workflows_is_unchanged():
    assert {path.name for path in _pull_request_workflows()} == BASE_PULL_REQUEST_WORKFLOWS


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_workflow_parses_and_declares_jobs(path: Path):
    document = _load(path)

    assert document["on"], f"{path.name} declares no trigger"
    assert document["jobs"], f"{path.name} declares no job"
