"""Seal the dedicated live Retention responsive/accessibility acceptance run."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

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
from .retention_visual_matrix_checkpoint import (
    _canonical,
    _decode_report,
    _load_json,
    _safe_relative,
    _source_file,
    _step_titles,
    _validate_url,
)
from .workflow3_lifecycle_evidence import STATE_ROOT, WORKTREE, _tree_digest
from .workflow3_lifecycle_evidence import _request as _runtime_request

SOURCE_ROOT = STATE_ROOT / "evidence/retention-dedicated-live-20260825-1"
ROOT = STATE_ROOT / "evidence/retention-dedicated-checkpoint-20260825-1"

ROUTE = "/console/retention/"
TEST_FILE = "retention-visual-accessibility-live.spec.ts"
TEST_TITLE = "authorized Retention UI reflows, focuses, and remains accessible"
PROJECTS = (
    "desktop-1440",
    "webkit-1440",
    "desktop-1280",
    "tablet-768",
    "mobile-390",
)
VIEWPORTS = {
    "desktop-1440": {"width": 1440, "height": 900},
    "webkit-1440": {"width": 1440, "height": 900},
    "desktop-1280": {"width": 1280, "height": 800},
    "tablet-768": {"width": 768, "height": 1024},
    "mobile-390": {"width": 390, "height": 844},
}
ACCEPTED_CRITERIA = (
    "product.retention.responsive-and-zoom",
    "product.retention.webkit-axe-and-keyboard",
    "ui.no-document-overflow",
    "ui.zoom-200-percent",
)
RUNTIME_PATHS = (
    "/health",
    "/v1/identity",
    "/v1/retention/policy",
    "/v1/retention/legal-holds",
)

GEOMETRY_IDS = (
    "policy-card",
    "legal-holds-card",
    "erasure-card",
    "retention-enabled",
    "run-ttl",
    "audit-ttl",
    "save-policy",
    "release-hold-0",
    "legal-run",
    "legal-reason",
    "place-hold",
    "single-run",
    "entire-tenant",
    "erasure-run",
    "erasure-note",
    "stage-erasure",
)
FOCUS_SEQUENCE = (
    ("input", "Run payloads TTL in days", "retention.policy.run-payloads-ttl"),
    ("input", "Audit records TTL in days", "retention.policy.audit-records-ttl"),
    (
        "button",
        f"Release legal hold {HOLD_ID}",
        f"retention.legal-holds.release.{HOLD_ID}",
    ),
    ("input", "Legal hold run ID", "retention.legal-holds.run-id"),
    ("input", "Legal hold reason", "retention.legal-holds.reason"),
    ("button", None, "retention.legal-holds.place"),
    ("button", "single run", "retention.erasure.scope.run"),
    ("button", "entire tenant", "retention.erasure.scope.tenant"),
    ("input", None, "retention.erasure.run-id"),
    ("input", None, "retention.erasure.note"),
)

_TOP_LEVEL_COUNTS = {
    "accessibility": 5,
    "console": 16,
    "network": 5,
    "playwright-report": 1,
    "screenshots": 6,
    "videos": 10,
}
_BODY_NAMES = (
    "retention-viewport-role-tenant",
    "retention-keyboard-focus",
    "retention-axe-wcag22-aa",
    "retention-sanitized-network",
    "retention-sanitized-console",
    "retention-zoom-200-geometry",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

Request = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]


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
        raise RuntimeError("source result criteria do not match the dedicated allowlist")
    return rows


def _references(row: Mapping[str, Any], *, declared: set[str]) -> tuple[str, ...]:
    value = row.get("evidence")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item in declared for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeError("criterion evidence is missing, duplicate, or undeclared")
    return tuple(value)


def _test_ids(row: Mapping[str, Any]) -> set[str]:
    value = row.get("test_id")
    if not isinstance(value, str) or not value:
        raise RuntimeError("criterion test identity is missing")
    result = value.split(",")
    if any(not item for item in result) or len(result) != len(set(result)):
        raise RuntimeError("criterion test identity is malformed")
    return set(result)


def _validate_criteria(
    rows: Mapping[str, Mapping[str, Any]], declared: set[str]
) -> dict[str, tuple[str, ...]]:
    evidence = {name: _references(row, declared=declared) for name, row in rows.items()}
    all_run = {item for item in declared if not item.startswith("playwright-report/")}
    common = set(evidence["product.retention.responsive-and-zoom"])
    if common != all_run or set(evidence["ui.no-document-overflow"]) != all_run:
        raise RuntimeError("dedicated common evidence does not cover the exact run")
    if len(common) != 42:
        raise RuntimeError("dedicated run artifact count is not exact")
    category_expectations = {
        "product.retention.webkit-axe-and-keyboard": {
            "accessibility": 1,
            "console": 3,
            "network": 1,
            "screenshots": 1,
            "videos": 2,
        },
        "ui.zoom-200-percent": {
            "accessibility": 1,
            "console": 4,
            "network": 1,
            "screenshots": 2,
            "videos": 2,
        },
    }
    for criterion, expected in category_expectations.items():
        references = set(evidence[criterion])
        if (
            not references <= common
            or Counter(Path(item).parts[0] for item in references) != expected
        ):
            raise RuntimeError(f"dedicated criterion evidence is incomplete: {criterion}")
    if set(evidence["product.retention.webkit-axe-and-keyboard"]) & set(
        evidence["ui.zoom-200-percent"]
    ):
        raise RuntimeError("WebKit and desktop zoom evidence must be independent")
    return evidence


def _validate_geometry(value: object, *, expected_ids: Sequence[str]) -> None:
    if not isinstance(value, list) or tuple(
        row.get("id") for row in value if isinstance(row, Mapping)
    ) != tuple(expected_ids):
        raise RuntimeError("Retention geometry targets are incomplete or out of order")
    keys = {
        "id",
        "x",
        "y",
        "width",
        "height",
        "right",
        "document_width",
        "clipped_by_ancestor",
        "horizontally_in_document",
        "has_area",
    }
    for row in value:
        if not isinstance(row, Mapping) or set(row) != keys:
            raise RuntimeError("Retention geometry evidence is malformed")
        numbers = [
            row.get(name) for name in ("x", "y", "width", "height", "right", "document_width")
        ]
        if (
            any(
                not isinstance(number, (int, float)) or isinstance(number, bool)
                for number in numbers
            )
            or row["width"] <= 0
            or row["height"] <= 0
            or row.get("clipped_by_ancestor") is not None
            or row.get("horizontally_in_document") is not True
            or row.get("has_area") is not True
        ):
            raise RuntimeError("Retention controls are clipped, overflowing, or absent")


def _validate_viewport(value: object) -> str:
    expected_keys = {
        "project",
        "viewport",
        "tenant_id",
        "workspace_id",
        "role",
        "deployment_ref",
        "deployment_version",
        "graph_version_ref",
        "geometry",
        "target_sizes",
        "document",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise RuntimeError("viewport role/tenant evidence is malformed")
    project = value.get("project")
    if not isinstance(project, str) or value.get("viewport") != VIEWPORTS.get(project):
        raise RuntimeError("viewport project dimensions are not exact")
    if {
        "tenant_id": value.get("tenant_id"),
        "workspace_id": value.get("workspace_id"),
        "role": value.get("role"),
        "deployment_ref": value.get("deployment_ref"),
        "deployment_version": value.get("deployment_version"),
        "graph_version_ref": value.get("graph_version_ref"),
    } != {
        "tenant_id": TENANT,
        "workspace_id": None,
        "role": ROLE,
        "deployment_ref": DEPLOYMENT,
        "deployment_version": 6,
        "graph_version_ref": GRAPH,
    }:
        raise RuntimeError("viewport identity is not exact platform_admin W1 v6 graph@4")
    _validate_geometry(value.get("geometry"), expected_ids=GEOMETRY_IDS)
    targets = value.get("target_sizes")
    if not isinstance(targets, list) or len(targets) != 11:
        raise RuntimeError("target-size evidence is incomplete")
    for row in targets:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"tag", "name", "width", "height", "meets_minimum"}
            or not isinstance(row.get("tag"), str)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("width"), (int, float))
            or isinstance(row.get("width"), bool)
            or not isinstance(row.get("height"), (int, float))
            or isinstance(row.get("height"), bool)
            or row["width"] < 24
            or row["height"] < 24
            or row.get("meets_minimum") is not True
        ):
            raise RuntimeError("target-size evidence does not meet the 24px minimum")
    document = value.get("document")
    if (
        not isinstance(document, Mapping)
        or set(document) != {"client_width", "scroll_width", "reduced_motion"}
        or document.get("scroll_width") != document.get("client_width")
        or document.get("reduced_motion") is not True
    ):
        raise RuntimeError("document overflow or reduced-motion evidence is invalid")
    return project


def _validate_focus(value: object) -> None:
    if not isinstance(value, list) or len(value) != len(FOCUS_SEQUENCE):
        raise RuntimeError("deterministic focus-visible evidence is incomplete")
    for row, expected in zip(value, FOCUS_SEQUENCE, strict=True):
        if (
            not isinstance(row, Mapping)
            or set(row) != {"tag", "aria_label", "evidence_id", "focus_visible"}
            or (row.get("tag"), row.get("aria_label"), row.get("evidence_id")) != expected
            or row.get("focus_visible") is not True
        ):
            raise RuntimeError("deterministic focus-visible evidence does not match")


def _validate_network(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "requests",
        "responses",
        "failed_responses",
    }:
        raise RuntimeError("sanitized network evidence is malformed")
    requests = value["requests"]
    responses = value["responses"]
    if (
        not isinstance(requests, list)
        or not requests
        or not isinstance(responses, list)
        or not responses
    ):
        raise RuntimeError("sanitized network evidence is incomplete")
    if value["failed_responses"] != []:
        raise RuntimeError("network evidence contains failed responses")
    request_urls: set[str] = set()
    for row in requests:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"method", "url", "resource_type"}
            or row.get("method") != "GET"
            or not isinstance(row.get("resource_type"), str)
        ):
            raise RuntimeError("network evidence contains a retention mutation")
        request_urls.add(_validate_url(row.get("url")))
    response_urls: set[str] = set()
    for row in responses:
        status = row.get("status") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or set(row) != {"url", "status", "resource_type"}
            or not isinstance(status, int)
            or isinstance(status, bool)
            or status >= 400
            or not isinstance(row.get("resource_type"), str)
        ):
            raise RuntimeError("network evidence contains failed responses")
        response_urls.add(_validate_url(row.get("url")))
    required = {
        "http://127.0.0.1:3000/console/retention/",
        "http://127.0.0.1:8122/health",
        "http://127.0.0.1:8122/v1/identity",
        "http://127.0.0.1:8122/v1/retention/policy",
        "http://127.0.0.1:8122/v1/retention/legal-holds",
    }
    if not required <= request_urls or not required <= response_urls:
        raise RuntimeError("network evidence does not prove the Retention route and APIs")


def _validate_console(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "events",
        "errors",
        "page_errors",
        "unhandled_rejections",
    }:
        raise RuntimeError("sanitized console evidence is malformed")
    if value["errors"] != [] or value["page_errors"] != [] or value["unhandled_rejections"] != 0:
        raise RuntimeError("console, page, or unhandled errors are present")
    events = value["events"]
    if not isinstance(events, list):
        raise RuntimeError("sanitized console events are malformed")
    for row in events:
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
            raise RuntimeError("console evidence is unsafe or contains an error")


def _validate_zoom(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "project",
        "zoom_percent",
        "geometry",
        "document",
    }:
        raise RuntimeError("200 percent zoom evidence is malformed")
    if value.get("project") != "desktop-1440" or value.get("zoom_percent") != 200:
        raise RuntimeError("200 percent zoom was not desktop-1440-only")
    _validate_geometry(value.get("geometry"), expected_ids=GEOMETRY_IDS[:3])
    document = value.get("document")
    if (
        not isinstance(document, Mapping)
        or set(document) != {"client_width", "scroll_width"}
        or not isinstance(document.get("client_width"), (int, float))
        or not isinstance(document.get("scroll_width"), (int, float))
        or document["scroll_width"] > document["client_width"] + 1
    ):
        raise RuntimeError("200 percent zoom has document overflow")


def _validate_indexed_artifacts(
    artifacts: Sequence[SourceArtifact],
) -> dict[str, Counter[str]]:
    bodies = {name: Counter() for name in _BODY_NAMES}
    viewport_projects: list[str] = []
    for artifact in artifacts:
        name = artifact.destination.name
        if name.endswith("retention-viewport-role-tenant.json"):
            value = _load_json(artifact.source, label=name)
            viewport_projects.append(_validate_viewport(value))
            key = "retention-viewport-role-tenant"
        elif name.endswith("retention-keyboard-focus.json"):
            value = _load_json(artifact.source, label=name)
            _validate_focus(value)
            key = "retention-keyboard-focus"
        elif name.endswith("retention-axe-wcag22-aa.json"):
            value = _load_json(artifact.source, label=name)
            if value != []:
                raise RuntimeError("WCAG 2.2 AA axe violations are present")
            key = "retention-axe-wcag22-aa"
        elif name.endswith("retention-sanitized-network.json"):
            value = _load_json(artifact.source, label=name)
            _validate_network(value)
            key = "retention-sanitized-network"
        elif name.endswith("retention-sanitized-console.json"):
            value = _load_json(artifact.source, label=name)
            _validate_console(value)
            key = "retention-sanitized-console"
        elif name.endswith("retention-zoom-200-geometry.json"):
            value = _load_json(artifact.source, label=name)
            _validate_zoom(value)
            key = "retention-zoom-200-geometry"
        else:
            key = ""
            value = None
            category = artifact.destination.parts[0]
            payload = artifact.source.read_bytes()
            if category == "screenshots" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("invalid dedicated Retention screenshot")
            if category == "videos" and not payload.startswith(b"\x1aE\xdf\xa3"):
                raise RuntimeError("invalid dedicated Retention video")
        if key:
            bodies[key][_canonical(value)] += 1
    if tuple(viewport_projects) != PROJECTS and set(viewport_projects) != set(PROJECTS):
        raise RuntimeError("viewport attachments do not prove the exact five projects")
    expected_counts = {
        "retention-viewport-role-tenant": 5,
        "retention-keyboard-focus": 5,
        "retention-axe-wcag22-aa": 5,
        "retention-sanitized-network": 5,
        "retention-sanitized-console": 5,
        "retention-zoom-200-geometry": 1,
    }
    if {name: sum(counter.values()) for name, counter in bodies.items()} != expected_counts:
        raise RuntimeError("indexed dedicated attachment inventory is incomplete")
    return bodies


def _flatten_steps(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[Mapping[str, Any]] = []
    for row in value:
        if isinstance(row, Mapping):
            result.append(row)
            result.extend(_flatten_steps(row.get("steps")))
    return result


def _validate_checkbox_restoration(steps: object) -> None:
    flattened = _flatten_steps(steps)
    matching = [
        row
        for row in flattened
        if isinstance(row.get("location"), Mapping)
        and row["location"].get("file") == TEST_FILE
        and row["location"].get("line") in {282, 283, 284, 285, 286}
    ]
    if [row.get("title") for row in matching] != [
        "Focus getByRole('checkbox', { name: 'Retention enforcement enabled' })",
        'Press "Space"',
        'Expect "toBe"',
        'Press "Space"',
        'Expect "toBe"',
    ]:
        raise RuntimeError("checkbox restoration steps are absent")
    snippets = "\n".join(str(row.get("snippet", "")) for row in matching)
    for required in (
        "const originallyChecked = await enabled.isChecked()",
        "expect(await enabled.isChecked()).toBe(!originallyChecked)",
        "expect(await enabled.isChecked()).toBe(originallyChecked)",
    ):
        if required not in snippets:
            raise RuntimeError("checkbox restoration assertions are absent")


def _validate_report(
    report_path: Path,
    *,
    rows: Mapping[str, Mapping[str, Any]],
    indexed_bodies: Mapping[str, Counter[str]],
    report_data_files: set[str],
) -> None:
    summary, detail = _decode_report(report_path)
    files = summary.get("files")
    stats = summary.get("stats")
    expected_stats = {
        "total": 5,
        "expected": 5,
        "unexpected": 0,
        "flaky": 0,
        "skipped": 0,
        "ok": True,
    }
    if (
        summary.get("projectNames") != list(PROJECTS)
        or summary.get("errors") != []
        or not isinstance(stats, Mapping)
        or {key: stats.get(key) for key in expected_stats} != expected_stats
        or not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], Mapping)
        or files[0].get("fileName") != TEST_FILE
    ):
        raise RuntimeError("Playwright report does not contain the exact dedicated matrix")
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

    embedded = {name: Counter() for name in indexed_bodies}
    attached_files: set[str] = set()
    project_test_ids: dict[str, str] = {}
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
            raise RuntimeError("Playwright report dedicated test identity is invalid")
        project_test_ids[project] = test_id
        annotations = test.get("annotations")
        if not isinstance(annotations, list):
            raise RuntimeError("Playwright report criteria are missing")
        observed = {
            row.get("description")
            for row in annotations
            if isinstance(row, Mapping) and row.get("type") == "criterion"
        }
        expected = {
            "product.retention.responsive-and-zoom",
            "ui.no-document-overflow",
        }
        if project == "webkit-1440":
            expected.add("product.retention.webkit-axe-and-keyboard")
        if project == "desktop-1440":
            expected.add("ui.zoom-200-percent")
        if observed != expected:
            raise RuntimeError("Playwright report dedicated criteria do not match")
        results = detail_test.get("results")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], Mapping)
        ):
            raise RuntimeError("Playwright report test result is malformed")
        if [
            title
            for title in _step_titles(results[0].get("steps"))
            if title.startswith("Navigate to ")
        ] != [f'Navigate to "{ROUTE}"']:
            raise RuntimeError("Playwright report route is not exact")
        _validate_checkbox_restoration(results[0].get("steps"))
        attachments = results[0].get("attachments")
        if not isinstance(attachments, list) or not all(
            isinstance(item, Mapping) for item in attachments
        ):
            raise RuntimeError("Playwright report attachments are malformed")
        expected_names = Counter(
            {
                "retention-visual-accessibility": 1,
                "retention-viewport-role-tenant": 1,
                "retention-keyboard-focus": 1,
                "retention-axe-wcag22-aa": 1,
                "retention-sanitized-network": 1,
                "retention-sanitized-console": 1,
                "video": 2,
            }
        )
        if project == "desktop-1440":
            expected_names["retention-zoom-200"] = 1
            expected_names["retention-zoom-200-geometry"] = 1
        if Counter(item.get("name") for item in attachments) != expected_names:
            raise RuntimeError("Playwright report project attachments are not exact")
        for attachment in attachments:
            name = attachment.get("name")
            if name in embedded:
                body = attachment.get("body")
                try:
                    value = json.loads(body) if isinstance(body, str) else None
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Playwright report attachment body is malformed") from exc
                embedded[name][_canonical(value)] += 1
            else:
                path = attachment.get("path")
                if not isinstance(path, str) or path in attached_files:
                    raise RuntimeError("Playwright report file attachment is malformed")
                attached_files.add(path)
    if embedded != indexed_bodies:
        raise RuntimeError("Playwright report bodies do not match indexed attachments")
    if attached_files != report_data_files:
        raise RuntimeError("Playwright report file inventory is not exact")
    all_ids = set(project_test_ids.values())
    for criterion in ("product.retention.responsive-and-zoom", "ui.no-document-overflow"):
        if _test_ids(rows[criterion]) != all_ids:
            raise RuntimeError("dedicated common criterion test IDs do not match")
    if _test_ids(rows["product.retention.webkit-axe-and-keyboard"]) != {
        project_test_ids["webkit-1440"]
    }:
        raise RuntimeError("dedicated WebKit criterion test ID does not match")
    if _test_ids(rows["ui.zoom-200-percent"]) != {project_test_ids["desktop-1440"]}:
        raise RuntimeError("200 percent zoom is not desktop-1440-only")


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
    if Counter(item.destination.parts[0] for item in artifacts) != _TOP_LEVEL_COUNTS:
        raise RuntimeError("source artifact counts do not match the dedicated run")
    _validate_criteria(rows, destinations)
    report_artifact = next(
        item for item in artifacts if item.destination.parts[0] == "playwright-report"
    )
    if report_artifact.destination != Path("playwright-report/index.html"):
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
        report_counts[relative.suffix.lower()] += 1
        if relative != Path("index.html"):
            report_data_files.add(relative.as_posix())
        destination = Path("playwright-report") / relative
        if destination.as_posix() not in destinations:
            artifacts.append(SourceArtifact(path, destination))
            destinations.add(destination.as_posix())
    if report_counts != {".html": 1, ".png": 6, ".webm": 10}:
        raise RuntimeError("Playwright report file inventory is not exact")
    indexed_bodies = _validate_indexed_artifacts(artifacts)
    _validate_report(
        report_artifact.source,
        rows=rows,
        indexed_bodies=indexed_bodies,
        report_data_files=report_data_files,
    )
    return SourceEvidence(results, tuple(artifacts))


def _validate_dedicated_runtime(records: Mapping[str, Any]) -> None:
    _validate_runtime(records)
    if records["health"].get("deployment_version") != 6:
        raise RuntimeError("health is not the exact W1 deployment version 6")


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate all source/runtime gates before creating the dedicated checkpoint."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    source = _load_source(source_root)
    runtime = _sanitize_runtime(request)
    _validate_dedicated_runtime(runtime)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "retention-dedicated-live",
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
            "deployment_version": 6,
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
            "viewport_attachment_count": 5,
            "zoom_geometry_attachment_count": 1,
            "failed_response_count": 0,
            "console_error_count": 0,
            "page_error_count": 0,
            "unhandled_rejection_count": 0,
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
    store.record_command(
        sequence=1,
        name="retention-dedicated-playwright",
        argv=[
            "npx",
            "playwright",
            "test",
            f"e2e/{TEST_FILE}",
            "--grep",
            f"^{TEST_TITLE}$",
            *[argument for project in PROJECTS for argument in ("--project", project)],
        ],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="5 dedicated Retention visual/accessibility tests passed.\n",
        stderr="",
    )
    evidence_paths.append("commands/0001-retention-dedicated-playwright.json")
    event_id = store.append_event(
        "campaign.retention_dedicated_verified",
        {
            "result": "pass",
            "project_count": 5,
            "zoom_project": "desktop-1440",
            "provider_call_count": 0,
            "mutation_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="retention-dedicated-live-20260825-1"),
    )
    common = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", common) for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Dedicated Retention responsive/accessibility checkpoint\n\n"
            "The exact dedicated Retention spec passed in five projects. The evidence "
            "proves platform_admin W1 deployment version 6 and graph@4, all three cards "
            "and controls without clipping or overflow, 24px minimum targets, deterministic "
            "visible focus, restored checkbox state, zero WCAG 2.2 AA violations, zero "
            "browser or response failures, and desktop-only 200 percent zoom. Fresh health, "
            "identity, policy, and legal-hold reads matched the service; no mutations or "
            "provider calls occurred.\n"
        ),
    )
    return destination


def _get(path: str) -> Any:
    if path not in RUNTIME_PATHS:
        raise RuntimeError("unexpected dedicated Retention checkpoint request path")
    return _runtime_request(path, method="GET")


def main() -> int:
    root = build_checkpoint(source_root=SOURCE_ROOT, destination=ROOT, request=_get)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
