"""Inert-by-default composition root for the live control-plane campaign.

The command-line surface can freeze evidence and run provider-free baseline gates.
Credential-bearing HTTP clients are accepted only through the Python composition
API, after the caller supplies the exact campaign-specific paid acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx

from .config import CampaignConfig
from .control_adapters import (
    DockerChromaInspector,
    HttpInstrumentedChromaCorpusSeeder,
    HttpPaidProbeExecutor,
    HttpSignedAuditInspector,
    InstrumentedChromaCorpusSeeder,
)
from .control_gate import ControlPlaneGate, PaidProbeExecutor, PaidProbeResult
from .control_gate_runtime import ControlGateRuntime
from .control_plane import (
    ControlPlaneEvidence,
    dirty_tree_hash,
    initialize_control_plane_evidence,
)
from .criteria import original_acceptance_criteria
from .evidence import EvidenceStore
from .ledger import CampaignLedger
from .runner import EvaluationReport, baseline_commands, execute_commands

_LOCAL_CONTROL_CRITERIA = (
    "control.tenant-budget-10",
    "control.run-budget-025",
    "control.budget-concurrency",
    "control.budget-rejection",
    "control.budget-commit-release",
    "control.budget-recovery",
    "control.audit-signed",
    "control.chroma-pinned-loopback",
    "control.chroma-corpus-seeded",
)


def paid_control_acknowledgement(campaign_id: str) -> str:
    """Return the exact acknowledgement required to cross paid control boundaries."""
    if not campaign_id:
        raise ValueError("campaign_id is required")
    return f"execute-exactly-two-paid-control-probes:{campaign_id}"


@dataclass(frozen=True)
class ControlPlaneOptions:
    repository_root: Path
    evidence_root: Path
    econ_database: Path
    sqlite_sources: Mapping[str, Path]
    runtime_versions: Mapping[str, str]
    browser_versions: Mapping[str, str]
    container_versions: Mapping[str, str]
    signing_reference: str
    chroma_container_name: str
    chroma_image: str
    chroma_host: str
    chroma_port: int
    provider_workflow_id: str
    chroma_connector_ref: str
    bundle_root: Path | None = None
    retain_raw_snapshots: bool = True


@dataclass(frozen=True, slots=True)
class ControlPlaneClients:
    """Injected runtime objects; auth headers remain owned by their HTTP clients."""

    service_client: httpx.Client
    chroma_client: httpx.Client
    chroma_collection: object | None = None
    embedding_executor: object | None = None
    chroma_seeder: object | None = None
    docker_command_runner: Callable[[tuple[str, ...]], tuple[int, str, str]] | None = None


@dataclass(frozen=True, slots=True)
class ControlPlaneRunResult:
    phase: Literal["awaiting_paid_acknowledgement", "control_complete", "control_ambiguous"]
    bundle_root: Path
    events: tuple[dict[str, object], ...]
    newly_executed_probes: tuple[PaidProbeResult, ...] = ()


BaselineRunner = Callable[..., EvaluationReport]


class ControlPlaneEntrypoint:
    """Prepare or resume one evidence bundle and execute only its control phase."""

    def __init__(
        self,
        *,
        campaign: CampaignConfig,
        options: ControlPlaneOptions,
        baseline_runner: BaselineRunner = execute_commands,
    ) -> None:
        self.campaign = campaign
        self.options = options
        self.baseline_runner = baseline_runner
        self.repository_root = options.repository_root.expanduser().resolve(strict=True)
        self._validate_external_roots()

    def _validate_external_roots(self) -> None:
        version_groups = (
            self.options.runtime_versions,
            self.options.browser_versions,
            self.options.container_versions,
        )
        if any(
            not group
            or any(not str(key).strip() or not str(value).strip() for key, value in group.items())
            for group in version_groups
        ):
            raise ValueError("runtime, browser, and container versions must be explicit")
        if not self.options.sqlite_sources:
            raise ValueError("at least one pretest SQLite source is required")
        candidates = {
            "campaign artifact root": self.campaign.artifact_root,
            "action sink root": self.campaign.action_sink_root,
            "evidence root": self.options.evidence_root,
            "economics database": self.options.econ_database,
        }
        for label, candidate in candidates.items():
            resolved = candidate.expanduser().resolve(strict=False)
            if resolved == self.repository_root or resolved.is_relative_to(self.repository_root):
                raise ValueError(f"{label} must be outside the repository")
        if self.options.bundle_root is not None:
            bundle = self.options.bundle_root.expanduser().resolve(strict=False)
            if bundle == self.repository_root or bundle.is_relative_to(self.repository_root):
                raise ValueError("bundle root must be outside the repository")

    def _resume_bundle(self, root: Path) -> ControlPlaneEvidence:
        bundle_root = root.expanduser().resolve(strict=True)
        store = EvidenceStore(bundle_root)
        if store.is_sealed:
            raise RuntimeError("control-plane evidence bundle is sealed")
        manifest_path = bundle_root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("resume bundle has no manifest")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("resume bundle manifest is malformed") from exc
        campaign_config = manifest.get("campaign_config")
        expected_campaign = self.campaign.model_dump(mode="json", exclude={"provider_secret_ref"})
        if campaign_config != expected_campaign:
            raise RuntimeError("resume bundle belongs to a different campaign")
        revision = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=self.repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if manifest.get("revision") != revision or manifest.get(
            "dirty_tree_hash"
        ) != dirty_tree_hash(self.repository_root):
            raise RuntimeError("repository revision or diff changed after evidence freeze")
        snapshots = manifest.get("pretest_sqlite_snapshots")
        attestation_reference = manifest.get("pretest_sqlite_snapshot_attestations")
        if isinstance(snapshots, list) and snapshots:
            for relative in snapshots:
                if not isinstance(relative, str):
                    raise RuntimeError("resume bundle snapshot inventory is malformed")
                snapshot = bundle_root / relative
                if not snapshot.is_file():
                    raise RuntimeError("resume bundle is missing a database snapshot")
                try:
                    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
                        result = connection.execute("PRAGMA integrity_check").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise RuntimeError("resume bundle contains an invalid SQLite snapshot") from exc
                if result != ("ok",):
                    raise RuntimeError("resume bundle contains a corrupt SQLite snapshot")
        elif attestation_reference == "database-snapshots/closed-snapshot-attestations.json":
            try:
                attestation = json.loads((bundle_root / attestation_reference).read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("resume bundle snapshot attestation is malformed") from exc
            rows = attestation.get("snapshots") if isinstance(attestation, dict) else None
            if (
                attestation.get("schema_version") != 1
                or attestation.get("raw_snapshots_in_bundle") is not False
                or not isinstance(rows, list)
                or not rows
                or any(
                    not isinstance(row, dict)
                    or row.get("quick_check") != "ok"
                    or row.get("raw_snapshot_in_bundle") is not False
                    or not isinstance(row.get("sha256"), str)
                    or len(row["sha256"]) != 64
                    or not isinstance(row.get("size_bytes"), int)
                    or row["size_bytes"] <= 0
                    or not isinstance(row.get("table_count"), int)
                    or row["table_count"] < 0
                    for row in rows
                )
            ):
                raise RuntimeError("resume bundle snapshot attestation is invalid")
        else:
            raise RuntimeError("resume bundle has no database snapshots")
        store.scan_recursive()
        return ControlPlaneEvidence(store, original_acceptance_criteria())

    def _prepare_bundle(self) -> ControlPlaneEvidence:
        if self.options.bundle_root is not None:
            return self._resume_bundle(self.options.bundle_root)
        return initialize_control_plane_evidence(
            evidence_root=self.options.evidence_root,
            repository_root=self.repository_root,
            campaign=self.campaign,
            sqlite_sources=self.options.sqlite_sources,
            runtime_versions=self.options.runtime_versions,
            browser_versions=self.options.browser_versions,
            container_versions=self.options.container_versions,
            retain_raw_snapshots=self.options.retain_raw_snapshots,
        )

    @staticmethod
    def _recorded_baseline(store: EvidenceStore) -> set[str]:
        passed: set[str] = set()
        for path in (store.root / "commands").glob("*.json"):
            try:
                record = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError("baseline command evidence is malformed") from exc
            if record.get("exit_code") == 0 and isinstance(record.get("name"), str):
                passed.add(record["name"])
        return passed

    def _run_baseline_once(self, evidence: ControlPlaneEvidence) -> None:
        commands = baseline_commands(self.repository_root)
        expected = {command.name for command in commands}
        passed = self._recorded_baseline(evidence.store)
        if expected <= passed:
            return
        if passed & expected:
            raise RuntimeError("baseline was interrupted; refusing to repeat commands")
        report = self.baseline_runner(
            commands,
            artifact_root=self.campaign.artifact_root,
            evidence_store=evidence.store,
        )
        if not report.passed:
            raise RuntimeError("local baseline gate failed")

    @staticmethod
    def _statuses(ledger: CampaignLedger) -> dict[str, str]:
        return {item.criterion_id: item.status for item in ledger.criteria}

    @staticmethod
    def _signed_inspection_reference(store: EvidenceStore) -> str:
        matching = [
            event
            for event in store.read_events()
            if event.get("type") == "control.audit-readiness.inspected"
            and isinstance(event.get("data"), dict)
            and event["data"].get("state") == "signed"  # type: ignore[index]
        ]
        if len(matching) != 1 or not isinstance(matching[0].get("event_id"), str):
            raise RuntimeError("signed audit inspection evidence is not unique")
        return f"events.ndjson#{matching[0]['event_id']}"

    @staticmethod
    def _probe_state(store: EvidenceStore, kind: str) -> tuple[int, int]:
        authorized = reconciled = 0
        for event in store.read_events():
            data = event.get("data")
            if not isinstance(data, dict) or data.get("kind") != kind:
                continue
            if event.get("type") == "control.probe.authorized":
                authorized += 1
            elif event.get("type") == "control.probe.reconciled":
                reconciled += 1
        return authorized, reconciled

    def _execute_pending_probes(
        self,
        *,
        runtime: ControlGateRuntime,
        provider_probe: PaidProbeExecutor,
        chroma_probe: PaidProbeExecutor,
    ) -> tuple[PaidProbeResult, ...]:
        results: list[PaidProbeResult] = []
        for kind, executor in (("provider", provider_probe), ("chroma", chroma_probe)):
            authorized, reconciled = self._probe_state(runtime.gate.store, kind)
            if authorized == reconciled == 1:
                continue
            if authorized or reconciled:
                raise RuntimeError(
                    f"{kind} paid probe has an ambiguous durable state; refusing reexecution"
                )
            authorization = runtime.gate.authorize_paid_probe(
                kind=kind,  # type: ignore[arg-type]
                operation_id=f"control-probe:{self.campaign.campaign_id}:{kind}",
                run_id=f"control-run:{self.campaign.campaign_id}:{kind}",
            )
            result = executor.execute_paid_probe(authorization)
            runtime.gate.reconcile_paid_probe(result)
            results.append(result)
        return tuple(results)

    def run(
        self,
        *,
        paid_acknowledgement: str | None = None,
        clients: ControlPlaneClients | None = None,
    ) -> ControlPlaneRunResult:
        if paid_acknowledgement is not None:
            expected_acknowledgement = paid_control_acknowledgement(self.campaign.campaign_id)
            if paid_acknowledgement != expected_acknowledgement:
                raise ValueError("exact paid control acknowledgement is required")
            if clients is None:
                raise ValueError("paid control execution requires injected auth-bearing clients")
        evidence = self._prepare_bundle()
        self._run_baseline_once(evidence)
        if paid_acknowledgement is None:
            return ControlPlaneRunResult(
                "awaiting_paid_acknowledgement",
                evidence.root,
                evidence.store.read_events(),
            )
        assert clients is not None

        ledger = evidence.resume_ledger()
        gate = ControlPlaneGate(store=evidence.store, ledger=ledger, campaign=self.campaign)
        runtime = ControlGateRuntime(
            gate=gate,
            econ_database=self.options.econ_database,
            command_working_directory=self.repository_root,
        )
        inspector_arguments: dict[str, Any] = {
            "client": clients.chroma_client,
            "store": evidence.store,
            "container_name": self.options.chroma_container_name,
            "expected_image": self.options.chroma_image,
            "host": self.options.chroma_host,
            "port": self.options.chroma_port,
        }
        if clients.docker_command_runner is not None:
            inspector_arguments["command_runner"] = clients.docker_command_runner
        audit_inspector = HttpSignedAuditInspector(
            client=clients.service_client,
            store=evidence.store,
            signing_reference=self.options.signing_reference,
        )
        chroma_inspector = DockerChromaInspector(**inspector_arguments)
        chroma_seeder = clients.chroma_seeder
        if chroma_seeder is None:
            if clients.chroma_collection is None or clients.embedding_executor is None:
                raise ValueError("paid control execution requires a concrete corpus seeder")
            chroma_seeder = InstrumentedChromaCorpusSeeder(
                collection=clients.chroma_collection,
                embedding_executor=clients.embedding_executor,  # type: ignore[arg-type]
                embedding_model=self.campaign.embedding_model,
                campaign_id=self.campaign.campaign_id,
                store=evidence.store,
            )

        statuses = self._statuses(ledger)
        completed_local = [statuses[item] == "pass" for item in _LOCAL_CONTROL_CRITERIA]
        if all(completed_local):
            pass
        elif any(completed_local):
            raise RuntimeError("local control gate is partial; refusing unsafe replay")
        else:
            runtime.execute_local_gates(
                audit_inspector=audit_inspector,
                chroma_inspector=chroma_inspector,
                chroma_seeder=chroma_seeder,
            )

        signed_reference = self._signed_inspection_reference(evidence.store)
        common_probe = {
            "client": clients.service_client,
            "store": evidence.store,
            "signed_audit_evidence_reference": signed_reference,
            "provider_workflow_id": self.options.provider_workflow_id,
            "chroma_connector_ref": self.options.chroma_connector_ref,
            "max_cost_usd": Decimal("0.25"),
        }
        provider_probe = HttpPaidProbeExecutor(kind="provider", **common_probe)
        chroma_probe = HttpPaidProbeExecutor(kind="chroma", **common_probe)
        try:
            results = self._execute_pending_probes(
                runtime=runtime,
                provider_probe=provider_probe,
                chroma_probe=chroma_probe,
            )
        except RuntimeError as exc:
            if "ambiguous durable state" not in str(exc):
                raise
            return ControlPlaneRunResult(
                "control_ambiguous", evidence.root, evidence.store.read_events()
            )
        return ControlPlaneRunResult(
            "control_complete",
            evidence.root,
            evidence.store.read_events(),
            results,
        )


def _parse_mapping(items: Sequence[str], *, path_values: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise ValueError(f"expected NAME=VALUE, got {item!r}")
        result[key] = Path(value) if path_values else value
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    baseline_runner: BaselineRunner = execute_commands,
    http_client_factory: Callable[..., httpx.Client] = httpx.Client,
    docker_command_runner: Callable[[tuple[str, ...]], tuple[int, str, str]] | None = None,
) -> int:
    """Capture and baseline only; paid execution requires the composition API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--econ-database", type=Path, required=True)
    parser.add_argument("--sqlite", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--runtime-version", action="append", required=True)
    parser.add_argument("--browser-version", action="append", required=True)
    parser.add_argument("--container-version", action="append", required=True)
    parser.add_argument("--resume-bundle", type=Path)
    parser.add_argument("--attest-snapshots", action="store_true")
    parser.add_argument("--execute-paid-control", action="store_true")
    parser.add_argument("--paid-ack")
    parser.add_argument("--console-url", default="http://127.0.0.1:8000")
    parser.add_argument("--chroma-url", default="http://127.0.0.1:8121")
    parser.add_argument("--service-auth-env", default="ZEROTH_EVALUATION_SERVICE_API_KEY")
    parser.add_argument("--chroma-auth-env")
    parser.add_argument("--provider-workflow-id", default="evaluation-provider-bootstrap")
    parser.add_argument("--chroma-connector-ref", default="chroma")
    parser.add_argument("--signing-reference", default="evaluation.control.signing")
    parser.add_argument("--chroma-container-name", default="zeroth-evaluation-chroma")
    parser.add_argument("--chroma-image", default="chromadb/chroma:1.5.6")
    parser.add_argument("--chroma-host", default="127.0.0.1")
    parser.add_argument("--chroma-port", type=int, default=8121)
    args = parser.parse_args(argv)
    campaign = CampaignConfig.model_validate_json(args.campaign_config.read_text())
    options = ControlPlaneOptions(
        repository_root=args.repository_root,
        evidence_root=args.evidence_root,
        econ_database=args.econ_database,
        sqlite_sources=_parse_mapping(args.sqlite, path_values=True),
        runtime_versions=_parse_mapping(args.runtime_version),
        browser_versions=_parse_mapping(args.browser_version),
        container_versions=_parse_mapping(args.container_version),
        signing_reference=args.signing_reference,
        chroma_container_name=args.chroma_container_name,
        chroma_image=args.chroma_image,
        chroma_host=args.chroma_host,
        chroma_port=args.chroma_port,
        provider_workflow_id=args.provider_workflow_id,
        chroma_connector_ref=args.chroma_connector_ref,
        bundle_root=args.resume_bundle,
        retain_raw_snapshots=not args.attest_snapshots,
    )
    entrypoint = ControlPlaneEntrypoint(
        campaign=campaign,
        options=options,
        baseline_runner=baseline_runner,
    )
    if args.execute_paid_control:
        service_auth = os.environ.get(args.service_auth_env)
        if not service_auth:
            raise ValueError("service auth environment variable is unresolved")
        service = http_client_factory(
            base_url=args.console_url,
            headers={"X-API-Key": service_auth},
            timeout=30,
        )
        chroma_headers: dict[str, str] = {}
        if args.chroma_auth_env:
            chroma_auth = os.environ.get(args.chroma_auth_env)
            if not chroma_auth:
                raise ValueError("Chroma auth environment variable is unresolved")
            chroma_headers["Authorization"] = f"Bearer {chroma_auth}"
        chroma = http_client_factory(
            base_url=args.chroma_url,
            headers=chroma_headers,
            timeout=30,
        )
        store_root = args.resume_bundle
        if store_root is None:
            # The seeder receives the actual store after initialization through
            # the composition path, so paid CLI requires a prepared bundle.
            raise ValueError("paid control CLI requires --resume-bundle from an inert prepare run")
        store = EvidenceStore(store_root)
        seeder = HttpInstrumentedChromaCorpusSeeder(
            client=service,
            store=store,
            campaign_id=campaign.campaign_id,
            connector_ref=args.chroma_connector_ref,
            embedding_model=campaign.embedding_model,
            max_cost_usd=Decimal("0.25"),
        )
        result = entrypoint.run(
            paid_acknowledgement=args.paid_ack,
            clients=ControlPlaneClients(
                service_client=service,
                chroma_client=chroma,
                chroma_seeder=seeder,
                docker_command_runner=docker_command_runner,
            ),
        )
    else:
        if args.paid_ack is not None:
            raise ValueError("--paid-ack requires --execute-paid-control")
        result = entrypoint.run()
    print(json.dumps({"bundle_root": str(result.bundle_root), "phase": result.phase}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
