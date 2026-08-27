"""ZER-37: the REAL minted installation token is absent from every surface.

Sibling of ``test_observable_surfaces.py``. That harness plants a fabricated
``github_pat_`` canary and literal-asserts a fixed twelve-record artifact
evidence trace over the agent-service stack; wiring a git-fetching repository
run into it would have meant rebuilding that trace around an unrelated flow,
so this file reuses its two durable pieces instead -- the
:class:`CredentialLeakScanner` and the six-surface taxonomy -- over the full
repo-run bootstrap of ``tests/service/test_repo_run_flow.py``.

The canary here is not fabricated: it is the installation token the
:class:`FakeGitHubAPI` actually minted, which the checkout actually presented
over git smart-HTTP (the loopback server is armed to 401 anything but that
exact ``Basic`` credential, so a passing stage IS the proof of use). After the
staged checkout is executed end-to-end by the repo-run worker, the token and
BOTH its credential forms -- the raw value and the base64
``x-access-token:<token>`` Basic form git carries -- must appear in none of:

* the workload environment the author script ran under,
* logs (script stdout/stderr and everything the process logged),
* error text (the service's refusal messages, the run's failure fields),
* artifacts on disk (every byte the flow left under ``tmp_path``: the staged
  tree, the git cache, the bare fixture, and the SQLite store itself),
* the persisted audit payloads (the terminal :class:`NodeAuditRecord` plus the
  run and checkout rows), and
* what another tenant can observe of the run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from release.security.scan import CredentialLeakScanner
from tests.service.test_repo_run_flow import make_rig
from zeroth.integrations.execution.sandbox import SandboxManager
from zeroth.service.repositories.repo_models import RepoCheckoutState, RepoRunState
from zeroth.service.repositories.service import ScriptNotDeclaredError

_INSTALLATION_ID = 1
# The broker caches per (installation_id, repository short name) -- the same
# key CheckoutRequest.name uses -- so this pre-lease is the exact token the
# checkout pipeline will reuse from the cache.
_REPO_NAME = "widgets"


def _files_on_disk(root: Path) -> dict[str, bytes]:
    """Every regular file the flow left under ``root``, as raw bytes."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


async def _run_repo_flow_with_enforced_token(
    sqlite_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> tuple[list[str], dict[str, Any]]:
    """Drive checkout -> run -> audit with the git server enforcing the token.

    Returns the canaries (token, Basic credential, bare base64 credential) and
    the captured observable surfaces. The transport proof is asserted inline:
    the flow only reaches SUCCEEDED because every git request carried exactly
    the minted credential.
    """
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        # Lease the token the checkout will reuse from the broker cache, and
        # arm the loopback git server to refuse anything but its exact Basic
        # form -- the fixture behaves like a private repository.
        broker = rig.service._checkout_service._broker  # noqa: SLF001
        lease = await broker.lease(_INSTALLATION_ID, _REPO_NAME)
        token = lease.reveal()
        basic_credential = lease.basic_auth_header()
        assert token == rig.api.minted_tokens[-1]
        rig.server.set_expected_auth("acme", basic_credential)

        # Record what the author script's sandbox execution actually observed,
        # on whichever manager instance the per-run runner ends up using.
        sandbox_results: list[Any] = []
        original_run_locally = SandboxManager._run_locally

        def recording_run_locally(self: SandboxManager, **kwargs: Any) -> Any:
            result = original_run_locally(self, **kwargs)
            sandbox_results.append(result)
            return result

        monkeypatch.setattr(SandboxManager, "_run_locally", recording_run_locally)

        with caplog.at_level(logging.DEBUG):
            checkout, report = await rig.service.create_checkout(
                "default", None, rig.repo_id, ref="main"
            )
            assert report is None, report
            assert checkout.state is RepoCheckoutState.STAGED

            # An error surface produced on purpose: the refusal of an
            # undeclared script (its message must echo declared names only).
            with pytest.raises(ScriptNotDeclaredError) as refusal:
                await rig.service.create_run(
                    "default", None, checkout.id, script="exfiltrate", input_payload={}
                )

            run = await rig.service.create_run(
                "default", None, checkout.id, script="train", input_payload={"word": "hi"}
            )
            assert await rig.worker.run_once() is True

        finished = await rig.service.get_run("default", run.id)
        assert finished is not None
        assert finished.state is RepoRunState.SUCCEEDED, finished.failure_code
        consumed = await rig.service.get_checkout("default", checkout.id)
        assert consumed is not None and consumed.state is RepoCheckoutState.CONSUMED

        # Transport proof: the fetch really happened over HTTP carrying the
        # minted token, on every git request, and the single-use credential
        # was revoked upstream once the fetch settled.
        git_requests = rig.server.requests
        assert any(path.endswith("git-upload-pack") for path, _auth in git_requests)
        assert git_requests and all(
            auth == basic_credential for _path, auth in git_requests
        )
        assert token in rig.api.minted_tokens
        assert token in rig.api.revoked_tokens

        assert len(sandbox_results) == 1  # exactly the author-script execution
        workload = sandbox_results[0]
        audit_records = await rig.audit_repository.list_by_run(run.id)
        assert len(audit_records) == 1

        surfaces: dict[str, Any] = {
            "workload-environment": dict(workload.environment),
            "logs": {
                "stdout": workload.stdout,
                "stderr": workload.stderr,
                "records": [record.getMessage() for record in caplog.records],
            },
            "errors": {
                "script-refusal": str(refusal.value),
                "checkout-failure": (str(consumed.failure_code), consumed.failure_detail),
                "run-failure": finished.failure_code,
            },
            "artifacts": _files_on_disk(tmp_path),
            "audit-payloads": {
                "audit": [record.model_dump(mode="json") for record in audit_records],
                "run": finished.model_dump(mode="json"),
                "checkout": consumed.model_dump(mode="json"),
            },
            "other-tenant": {
                "run": repr(await rig.service.get_run("tenant-b", run.id)),
                "checkout": repr(await rig.service.get_checkout("tenant-b", checkout.id)),
                "checkouts": repr(await rig.service.list_checkouts("tenant-b")),
                "runs": repr(await rig.service.list_runs("tenant-b")),
            },
        }
        canaries = [token, basic_credential, basic_credential.split(" ", 1)[1]]
        return canaries, surfaces
    finally:
        await rig.aclose()


async def test_repo_run_fetches_over_http_with_the_minted_token(
    sqlite_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The flow itself: enforced-credential fetch, staged run, one audit row."""
    canaries, surfaces = await _run_repo_flow_with_enforced_token(
        sqlite_db, tmp_path, monkeypatch, caplog
    )
    # The captured surfaces are real evidence, not empty shells.
    assert surfaces["workload-environment"]
    assert surfaces["logs"]["stdout"]
    assert surfaces["artifacts"]
    assert "zeroth.db" in surfaces["artifacts"]
    assert any(name.endswith(".zeroth.yaml") for name in surfaces["artifacts"])
    assert surfaces["audit-payloads"]["audit"][0]["status"] == "completed"
    assert "train" in surfaces["errors"]["script-refusal"]
    assert "exfiltrate" not in surfaces["errors"]["script-refusal"]
    assert len(canaries) == 3


async def test_minted_token_and_basic_credential_absent_from_all_surfaces(
    sqlite_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Neither the token nor its Basic form survives on any observable surface."""
    canaries, surfaces = await _run_repo_flow_with_enforced_token(
        sqlite_db, tmp_path, monkeypatch, caplog
    )
    scanner = CredentialLeakScanner(canaries)
    for surface, captured in surfaces.items():
        findings = scanner.scan(captured, surface=surface)
        assert findings == [], f"credential observed on surface {surface!r}: {findings}"
