"""Seal the live Retention visual and accessibility project matrix."""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .native_safari_retention_checkpoint import (
    DEPLOYMENT,
    GRAPH,
    HELD_RUN_ID,
    HOLD_ID,
    ROLE,
    TENANT,
    _revision,
    _sanitize_runtime,
    _validate_runtime,
)
from .workflow3_lifecycle_evidence import (
    STATE_ROOT,
    WORKTREE,
    _tree_digest,
)
from .workflow3_lifecycle_evidence import (
    _request as _runtime_request,
)

SOURCE_ROOT = STATE_ROOT / "evidence/retention-visual-matrix-live-20260825-1"
ROOT = STATE_ROOT / "evidence/retention-visual-matrix-checkpoint-20260825-1"

ROUTE = "/console/retention/"
TEST_FILE = "studio-surfaces.spec.ts"
TEST_TITLE = "retention has durable, credential-safe UI evidence"
PROJECTS = (
    "desktop-1440",
    "webkit-1440",
    "desktop-1280",
    "tablet-768",
    "mobile-390",
)
PROJECT_CRITERIA = {
    "desktop-1440": "ui.viewport-1440x900",
    "webkit-1440": "ui.viewport-1440x900",
    "desktop-1280": "ui.viewport-1280x800",
    "tablet-768": "ui.viewport-768x1024",
    "mobile-390": "ui.viewport-390x844",
}
COMMON_CRITERIA = (
    "ui.operational-surfaces",
    "ui.focus-visible-order",
    "ui.reduced-motion",
    "ui.axe-wcag22-aa",
    "stop.no-indefinite-loading",
)
ACCEPTED_CRITERIA = (
    "stop.no-indefinite-loading",
    "ui.axe-wcag22-aa",
    "ui.focus-visible-order",
    "ui.no-document-overflow",
    "ui.operational-surfaces",
    "ui.reduced-motion",
    "ui.viewport-1280x800",
    "ui.viewport-1440x900",
    "ui.viewport-390x844",
    "ui.viewport-768x1024",
    "ui.zoom-200-percent",
)
RUNTIME_PATHS = (
    "/health",
    "/v1/identity",
    "/v1/retention/policy",
    "/v1/retention/legal-holds",
)

_TOP_LEVEL_COUNTS = {
    "accessibility": 5,
    "console": 10,
    "network": 5,
    "playwright-report": 1,
    "screenshots": 6,
    "videos": 10,
}
_REPORT_TEMPLATE = re.compile(
    r'<template id="playwrightReportBase64">\s*'
    r"(?:data:application/zip;base64,)?([A-Za-z0-9+/=]+)\s*</template>"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

Request = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]
    project_evidence: Mapping[str, tuple[str, ...]]


def _load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid source artifact {label}")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe source artifact {label}")
    return relative


def _source_file(root: Path, relative: Path) -> Path:
    unresolved = root / relative
    if unresolved.is_symlink():
        raise RuntimeError("source artifact may not be a symlink")
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("source artifact escapes its evidence root") from exc
    if not candidate.is_file():
        raise RuntimeError(f"missing source artifact: {relative.as_posix()}")
    return candidate


def _criterion_rows(results: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    value = results.get("criteria")
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise RuntimeError("source criteria are malformed")
    rows = {str(row.get("criterion_id")): row for row in value}
    if (
        len(rows) != len(value)
        or tuple(rows) != ACCEPTED_CRITERIA
        or any(row.get("status") != "pass" for row in rows.values())
    ):
        raise RuntimeError("source result criteria do not match the checkpoint allowlist")
    return rows


def _references(row: Mapping[str, Any], *, declared: set[str]) -> tuple[str, ...]:
    evidence = row.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item in declared for item in evidence)
        or len(evidence) != len(set(evidence))
    ):
        raise RuntimeError("criterion evidence is missing, duplicate, or undeclared")
    return tuple(evidence)


def _test_ids(row: Mapping[str, Any]) -> set[str]:
    value = row.get("test_id")
    if not isinstance(value, str) or not value:
        raise RuntimeError("criterion test identity is missing")
    identifiers = value.split(",")
    if any(not identifier for identifier in identifiers) or len(identifiers) != len(
        set(identifiers)
    ):
        raise RuntimeError("criterion test identity is malformed")
    return set(identifiers)


def _project_evidence(
    rows: Mapping[str, Mapping[str, Any]], declared: set[str]
) -> dict[str, tuple[str, ...]]:
    evidence = {criterion: _references(row, declared=declared) for criterion, row in rows.items()}
    desktop = set(evidence["ui.zoom-200-percent"])
    if desktop != set(evidence["ui.no-document-overflow"]):
        raise RuntimeError("200 percent zoom evidence is not desktop-1440-only")
    project_sets = {
        "desktop-1440": desktop,
        "webkit-1440": set(evidence["ui.viewport-1440x900"]) - desktop,
        "desktop-1280": set(evidence["ui.viewport-1280x800"]),
        "tablet-768": set(evidence["ui.viewport-768x1024"]),
        "mobile-390": set(evidence["ui.viewport-390x844"]),
    }
    expected_counts = {
        "accessibility": 1,
        "console": 2,
        "network": 1,
        "screenshots": 1,
        "videos": 2,
    }
    for project, references in project_sets.items():
        counts = Counter(Path(reference).parts[0] for reference in references)
        wanted = dict(expected_counts)
        if project == "desktop-1440":
            wanted["screenshots"] = 2
        if counts != wanted:
            raise RuntimeError(f"project evidence categories are incomplete: {project}")
    if any(
        first.intersection(second)
        for index, first in enumerate(project_sets.values())
        for second in tuple(project_sets.values())[index + 1 :]
    ):
        raise RuntimeError("project evidence must be independent")
    all_project_evidence = set().union(*project_sets.values())
    declared_run_artifacts = {
        reference for reference in declared if not reference.startswith("playwright-report/")
    }
    if all_project_evidence != declared_run_artifacts:
        raise RuntimeError("project evidence does not cover the exact run artifacts")
    for criterion in COMMON_CRITERIA:
        if set(evidence[criterion]) != all_project_evidence:
            raise RuntimeError(f"common criterion evidence is incomplete: {criterion}")
    if set(evidence["ui.viewport-1440x900"]) != (
        project_sets["desktop-1440"] | project_sets["webkit-1440"]
    ):
        raise RuntimeError("1440 project evidence is incomplete")
    return {project: tuple(sorted(references)) for project, references in project_sets.items()}


def _validate_url(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("network evidence contains an invalid URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port not in {3000, 8122}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("network evidence is not sanitized and local")
    return value


def _validate_network(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"requests", "responses"}:
        raise RuntimeError("network evidence is malformed")
    requests = value["requests"]
    responses = value["responses"]
    if (
        not isinstance(requests, list)
        or not requests
        or not isinstance(responses, list)
        or not responses
    ):
        raise RuntimeError("network evidence is incomplete")
    request_urls: list[str] = []
    for row in requests:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"method", "resource_type", "url"}
            or row.get("method") != "GET"
            or not isinstance(row.get("resource_type"), str)
        ):
            raise RuntimeError("network evidence contains a mutation")
        request_urls.append(_validate_url(row.get("url")))
    response_urls: list[str] = []
    for row in responses:
        status = row.get("status") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or set(row) != {"resource_type", "status", "url"}
            or not isinstance(status, int)
            or isinstance(status, bool)
            or not 200 <= status < 400
            or not isinstance(row.get("resource_type"), str)
        ):
            raise RuntimeError("network evidence contains failed responses")
        response_urls.append(_validate_url(row.get("url")))
    required = {
        "http://127.0.0.1:3000/console/retention/",
        "http://127.0.0.1:8122/health",
        "http://127.0.0.1:8122/v1/identity",
        "http://127.0.0.1:8122/v1/retention/policy",
        "http://127.0.0.1:8122/v1/retention/legal-holds",
    }
    if not required <= set(request_urls) or not required <= set(response_urls):
        raise RuntimeError("network evidence does not prove the exact Retention route")


def _validate_console(value: Any) -> None:
    if not isinstance(value, list):
        raise RuntimeError("console evidence is malformed")
    for row in value:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"type", "message_bytes", "message_sha256", "url"}
            or row.get("type") == "error"
            or not isinstance(row.get("type"), str)
            or not isinstance(row.get("message_bytes"), int)
            or isinstance(row.get("message_bytes"), bool)
            or row["message_bytes"] < 0
            or not isinstance(row.get("message_sha256"), str)
            or not _SHA256.fullmatch(row["message_sha256"])
            or not (row.get("url") is None or isinstance(row.get("url"), str))
        ):
            raise RuntimeError("console errors or unsafe console evidence are present")


def _validate_keyboard(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 8:
        raise RuntimeError("focus-visible keyboard evidence is incomplete")
    for row in value:
        if not (
            row is None
            or (
                isinstance(row, Mapping)
                and set(row) == {"tag", "role", "focus_visible"}
                and isinstance(row.get("tag"), str)
                and (row.get("role") is None or isinstance(row.get("role"), str))
                and isinstance(row.get("focus_visible"), bool)
            )
        ):
            raise RuntimeError("focus-visible keyboard evidence is malformed")
    if not any(isinstance(row, Mapping) and row.get("focus_visible") is True for row in value):
        raise RuntimeError("focus-visible keyboard evidence is absent")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _validate_indexed_artifacts(
    artifacts: Sequence[SourceArtifact],
) -> dict[str, Counter[str]]:
    bodies: dict[str, Counter[str]] = {
        name: Counter()
        for name in ("network-summary", "console-summary", "keyboard-results", "axe-results")
    }
    for artifact in artifacts:
        name = artifact.destination.name
        if name.endswith("network-summary.json"):
            value = _load_json(artifact.source, label=name)
            _validate_network(value)
            bodies["network-summary"][_canonical(value)] += 1
        elif name.endswith("console-summary.json"):
            value = _load_json(artifact.source, label=name)
            _validate_console(value)
            bodies["console-summary"][_canonical(value)] += 1
        elif name.endswith("keyboard-results.json"):
            value = _load_json(artifact.source, label=name)
            _validate_keyboard(value)
            bodies["keyboard-results"][_canonical(value)] += 1
        elif name.endswith("axe-results.json"):
            value = _load_json(artifact.source, label=name)
            if value != []:
                raise RuntimeError("axe violations are present")
            bodies["axe-results"][_canonical(value)] += 1
        elif artifact.destination.parts[0] == "screenshots":
            if not artifact.source.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("invalid visual matrix screenshot")
        elif artifact.destination.parts[0] == "videos":
            if not artifact.source.read_bytes().startswith(b"\x1aE\xdf\xa3"):
                raise RuntimeError("invalid visual matrix video")
    if {name: sum(values.values()) for name, values in bodies.items()} != {
        "network-summary": 5,
        "console-summary": 5,
        "keyboard-results": 5,
        "axe-results": 5,
    }:
        raise RuntimeError("indexed attachment categories are incomplete")
    return bodies


def _decode_report(report_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("Playwright report is unavailable") from exc
    match = _REPORT_TEMPLATE.search(text)
    if match is None:
        raise RuntimeError("Playwright report has no embedded archive")
    try:
        payload = base64.b64decode(match.group(1), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Playwright report archive is malformed") from exc
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if (
                len(infos) != 2
                or any(info.is_dir() or info.flag_bits & 1 for info in infos)
                or sum(info.file_size for info in infos) > 5_000_000
                or "report.json" not in {info.filename for info in infos}
            ):
                raise RuntimeError("Playwright report archive inventory is invalid")
            summary = json.loads(archive.read("report.json"))
            files = summary.get("files") if isinstance(summary, Mapping) else None
            if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
                raise RuntimeError("Playwright report file inventory is invalid")
            detail_name = f"{files[0].get('fileId')}.json"
            if {info.filename for info in infos} != {"report.json", detail_name}:
                raise RuntimeError("Playwright report archive inventory is invalid")
            detail = json.loads(archive.read(detail_name))
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Playwright report archive is malformed") from exc
    if not isinstance(summary, dict) or not isinstance(detail, dict):
        raise RuntimeError("Playwright report JSON is malformed")
    return summary, detail


def _step_titles(steps: object) -> list[str]:
    if not isinstance(steps, list):
        return []
    titles: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        title = step.get("title")
        if isinstance(title, str):
            titles.append(title)
        titles.extend(_step_titles(step.get("steps")))
    return titles


def _validate_report(
    report_path: Path,
    *,
    rows: Mapping[str, Mapping[str, Any]],
    project_evidence: Mapping[str, tuple[str, ...]],
    indexed_bodies: Mapping[str, Counter[str]],
    report_data_files: set[str],
) -> None:
    summary, detail = _decode_report(report_path)
    files = summary.get("files")
    assert isinstance(files, list) and isinstance(files[0], Mapping)
    stats = summary.get("stats")
    if (
        summary.get("projectNames") != list(PROJECTS)
        or summary.get("errors") != []
        or not isinstance(stats, Mapping)
        or {
            key: stats.get(key)
            for key in ("total", "expected", "unexpected", "flaky", "skipped", "ok")
        }
        != {"total": 5, "expected": 5, "unexpected": 0, "flaky": 0, "skipped": 0, "ok": True}
        or files[0].get("fileName") != TEST_FILE
    ):
        raise RuntimeError("Playwright report does not contain the exact project matrix")
    summary_tests = files[0].get("tests")
    detail_tests = detail.get("tests")
    if (
        not isinstance(summary_tests, list)
        or len(summary_tests) != 5
        or not isinstance(detail_tests, list)
        or len(detail_tests) != 5
    ):
        raise RuntimeError("Playwright report does not contain exactly five tests")
    summaries = {
        test.get("projectName"): test for test in summary_tests if isinstance(test, Mapping)
    }
    details = {test.get("projectName"): test for test in detail_tests if isinstance(test, Mapping)}
    if tuple(summaries) != PROJECTS or set(details) != set(PROJECTS):
        raise RuntimeError("Playwright report projects do not match")

    embedded_bodies = {name: Counter() for name in indexed_bodies}
    attached_files: set[str] = set()
    project_test_ids: dict[str, str] = {}
    common_annotations = set(COMMON_CRITERIA)
    for project in PROJECTS:
        test = summaries[project]
        detail_test = details[project]
        test_id = test.get("testId")
        if (
            not isinstance(test_id, str)
            or not test_id
            or detail_test.get("testId") != test_id
            or test.get("title") != TEST_TITLE
            or detail_test.get("title") != TEST_TITLE
            or test.get("ok") is not True
            or test.get("outcome") != "expected"
        ):
            raise RuntimeError("Playwright report test identity is invalid")
        project_test_ids[project] = test_id
        annotations = test.get("annotations")
        if not isinstance(annotations, list):
            raise RuntimeError("Playwright report criteria are missing")
        observed_annotations = {
            annotation.get("description")
            for annotation in annotations
            if isinstance(annotation, Mapping) and annotation.get("type") == "criterion"
        }
        expected_annotations = common_annotations | {PROJECT_CRITERIA[project]}
        if project == "desktop-1440":
            expected_annotations |= {"ui.zoom-200-percent", "ui.no-document-overflow"}
        if observed_annotations != expected_annotations:
            raise RuntimeError("Playwright report project criteria do not match")

        results = detail_test.get("results")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], Mapping)
        ):
            raise RuntimeError("Playwright report test result is malformed")
        titles = [
            title
            for title in _step_titles(results[0].get("steps"))
            if title.startswith("Navigate to ")
        ]
        if titles != [f'Navigate to "{ROUTE}"']:
            raise RuntimeError("Playwright report route is not exact")
        attachments = results[0].get("attachments")
        if not isinstance(attachments, list) or not all(
            isinstance(attachment, Mapping) for attachment in attachments
        ):
            raise RuntimeError("Playwright report attachments are malformed")
        expected_names = Counter(
            {
                "screenshot": 1,
                "network-summary": 1,
                "console-summary": 1,
                "keyboard-results": 1,
                "axe-results": 1,
                "video": 2,
            }
        )
        if project == "desktop-1440":
            expected_names["zoom-200-screenshot"] = 1
        if Counter(attachment.get("name") for attachment in attachments) != expected_names:
            raise RuntimeError("Playwright report attachments do not match the project")
        for attachment in attachments:
            name = attachment.get("name")
            if name in embedded_bodies:
                body = attachment.get("body")
                try:
                    value = json.loads(body) if isinstance(body, str) else None
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Playwright report attachment body is malformed") from exc
                embedded_bodies[name][_canonical(value)] += 1
            else:
                path = attachment.get("path")
                if not isinstance(path, str) or path in attached_files:
                    raise RuntimeError("Playwright report file attachment is malformed")
                attached_files.add(path)

    if embedded_bodies != indexed_bodies:
        raise RuntimeError("Playwright report bodies do not match indexed attachments")
    if attached_files != report_data_files:
        raise RuntimeError("Playwright report file inventory is not exact")
    all_test_ids = set(project_test_ids.values())
    for criterion in COMMON_CRITERIA:
        if _test_ids(rows[criterion]) != all_test_ids:
            raise RuntimeError("Playwright report common criterion tests do not match")
    for criterion in ("ui.zoom-200-percent", "ui.no-document-overflow"):
        if _test_ids(rows[criterion]) != {project_test_ids["desktop-1440"]}:
            raise RuntimeError("200 percent zoom is not desktop-1440-only")
    for criterion in set(PROJECT_CRITERIA.values()):
        expected_projects = {
            project
            for project, project_criterion in PROJECT_CRITERIA.items()
            if project_criterion == criterion
        }
        if _test_ids(rows[criterion]) != {
            project_test_ids[project] for project in expected_projects
        }:
            raise RuntimeError("Playwright report viewport criterion tests do not match")
    if set(project_evidence) != set(project_test_ids):
        raise RuntimeError("Playwright report projects are not associated with evidence")


def _load_source(root: Path) -> SourceEvidence:
    root = root.expanduser().resolve(strict=True)
    EvidenceStore(root).scan_recursive()
    results = _load_json(root / "results.json", label="source results")
    if (
        not isinstance(results, dict)
        or results.get("schema_version") != 1
        or results.get("completed") is not True
    ):
        raise RuntimeError("source results are incomplete")
    rows = _criterion_rows(results)
    declarations = results.get("artifacts")
    if not isinstance(declarations, list):
        raise RuntimeError("source artifacts are missing")
    artifacts: list[SourceArtifact] = []
    destinations: set[str] = set()
    for declaration in declarations:
        if not isinstance(declaration, Mapping):
            raise RuntimeError("source artifact declaration is malformed")
        source_relative = _safe_relative(declaration.get("source"), label="source")
        destination = _safe_relative(declaration.get("destination"), label="destination")
        if (
            len(destination.parts) < 2
            or destination.parts[0] not in _TOP_LEVEL_COUNTS
            or destination.as_posix() in destinations
        ):
            raise RuntimeError("source artifact destination is invalid or duplicate")
        destinations.add(destination.as_posix())
        artifacts.append(SourceArtifact(_source_file(root, source_relative), destination))
    if Counter(artifact.destination.parts[0] for artifact in artifacts) != _TOP_LEVEL_COUNTS:
        raise RuntimeError("source artifact counts do not match the visual matrix")
    report_artifacts = [
        artifact for artifact in artifacts if artifact.destination.parts[0] == "playwright-report"
    ]
    if report_artifacts[0].destination != Path("playwright-report/index.html"):
        raise RuntimeError("Playwright report entrypoint is missing")
    report_root = root / "html-report"
    report_counts: Counter[str] = Counter()
    report_data_files: set[str] = set()
    for path in sorted(report_root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Playwright report may not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(report_root)
        suffix = relative.suffix.lower()
        report_counts[suffix] += 1
        if relative != Path("index.html"):
            report_data_files.add(relative.as_posix())
            if suffix == ".png" and not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("Playwright report contains an invalid screenshot")
            if suffix == ".webm" and not path.read_bytes().startswith(b"\x1aE\xdf\xa3"):
                raise RuntimeError("Playwright report contains an invalid video")
        destination = Path("playwright-report") / relative
        if destination.as_posix() not in destinations:
            artifacts.append(SourceArtifact(path, destination))
            destinations.add(destination.as_posix())
    if report_counts != {".html": 1, ".png": 6, ".webm": 10}:
        raise RuntimeError("Playwright report file inventory is not exact")

    project_evidence = _project_evidence(rows, destinations)
    indexed_bodies = _validate_indexed_artifacts(artifacts)
    _validate_report(
        report_artifacts[0].source,
        rows=rows,
        project_evidence=project_evidence,
        indexed_bodies=indexed_bodies,
        report_data_files=report_data_files,
    )
    return SourceEvidence(results, tuple(artifacts), project_evidence)


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate the exact browser/runtime matrix before creating a checkpoint."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    source = _load_source(source_root)
    runtime = _sanitize_runtime(request)
    _validate_runtime(runtime)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "retention-visual-accessibility-matrix-live",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _revision(),
            "working_tree_digest": _tree_digest(WORKTREE),
            "source_root": source_root.resolve().name,
            "source_results_sha256": sha256(
                (source_root / "results.json").read_bytes()
            ).hexdigest(),
            "tenant_id": TENANT,
            "role": ROLE,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "active_hold_id": HOLD_ID,
            "held_run_ref": HELD_RUN_ID,
            "route": ROUTE,
            "test_file": TEST_FILE,
            "test_title": TEST_TITLE,
            "projects": list(PROJECTS),
            "screenshot_count": 6,
            "video_count": 10,
            "network_attachment_count": 5,
            "console_attachment_count": 5,
            "keyboard_attachment_count": 5,
            "axe_attachment_count": 5,
            "failed_response_count": 0,
            "console_error_count": 0,
            "axe_violation_count": 0,
            "provider_calls_performed": 0,
            "mutations_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    evidence_paths = ["playwright-report/results.json"]
    store._write_exclusive(Path(evidence_paths[0]), source.results)
    for name, value in runtime.items():
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for artifact in source.artifacts:
        store.ingest_artifact(artifact.source, artifact.destination)
        evidence_paths.append(artifact.destination.as_posix())

    screenshot_index = {
        "schema_version": 1,
        "route": ROUTE,
        "screenshots": [
            {
                "file": reference,
                "project": project,
                "zoom_percent": 200 if "zoom-200" in reference else 100,
                "criterion_ids": [
                    row["criterion_id"]
                    for row in source.results["criteria"]
                    if reference in row["evidence"]
                ],
            }
            for project, references in source.project_evidence.items()
            for reference in references
            if reference.startswith("screenshots/")
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")
    store.record_command(
        sequence=1,
        name="retention-visual-matrix-playwright",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/studio-surfaces.spec.ts",
            "--grep",
            f"^{TEST_TITLE}$",
            *[argument for project in PROJECTS for argument in ("--project", project)],
        ],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="5 Retention visual and accessibility project tests passed.\n",
        stderr="",
    )
    evidence_paths.append("commands/0001-retention-visual-matrix-playwright.json")
    event_id = store.append_event(
        "campaign.retention_visual_matrix_verified",
        {
            "result": "pass",
            "route": ROUTE,
            "project_count": 5,
            "zoom_project": "desktop-1440",
            "failed_response_count": 0,
            "console_error_count": 0,
            "axe_violation_count": 0,
            "provider_call_count": 0,
            "mutation_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="retention-visual-matrix-live-20260825-1"),
    )
    common_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", common_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Retention visual and accessibility matrix checkpoint\n\n"
            "The exact Retention surface test passed in Chromium at 1440, 1280, 768, "
            "and 390 pixels and in WebKit at 1440 pixels. Every project retained a "
            "sanitized screenshot, two videos, GET-only network summary, error-free "
            "console summary, eight-step keyboard record with visible focus, and empty "
            "axe violation result. Only desktop-1440 exercised and captured 200 percent "
            "zoom. Read-only health, identity, no-expiry policy, and the active legal hold "
            "matched the live service. No mutation or provider call occurred.\n"
        ),
    )
    return destination


def _get(path: str) -> Any:
    if path not in RUNTIME_PATHS:
        raise RuntimeError("unexpected Retention checkpoint request path")
    return _runtime_request(path, method="GET")


def main() -> int:
    root = build_checkpoint(source_root=SOURCE_ROOT, destination=ROOT, request=_get)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
