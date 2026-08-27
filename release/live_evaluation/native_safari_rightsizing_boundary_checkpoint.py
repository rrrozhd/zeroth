"""Fail-closed sealer for native Safari Rightsizing client-boundary evidence."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .control_plane import dirty_tree_hash
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

TENANT = "evaluation-studio-v1"
EXACT_ERROR = "Tolerance must be a number from 0 through 100."
EXPECTED_D012 = {
    "status": "ok",
    "campaign_id": TENANT,
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}
SCREENSHOTS = (
    "01-configured-native-safari.png",
    "02-tolerance-101-error-native-safari.png",
)
ACCESSIBILITY = (
    "01-configured-native-safari.txt",
    "02-tolerance-101-error-native-safari.txt",
)
RUNTIME = ("observation.json", "before.json", "after.json", "health.json")
CRITERIA = (
    "rightsizing.native-safari-boundary-configured",
    "rightsizing.tolerance-101-client-validation",
    "rightsizing.boundary-submit-zero-side-effects",
    "rightsizing.native-safari-identity",
    "rightsizing.boundary-d012-health-preserved",
    "audit.zero-secrets",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"missing {label}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    EvidenceStore(path.parent.parent).validate(value)
    return value


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _whole_number(value: object, *, minimum: int, maximum: int) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = int(str(value))
    except ValueError:
        return False
    return str(parsed) == str(value) and minimum <= parsed <= maximum


def _validate_observation(value: Mapping[str, Any]) -> dict[str, object]:
    browser = value.get("browser")
    configured = value.get("configured_fields")
    submission = value.get("submission")
    if (
        value.get("schema_version") != 1
        or value.get("route") != "/console/rightsizing/"
        or not isinstance(browser, Mapping)
        or browser.get("name") != "Safari"
        or browser.get("engine") != "WebKit"
        or browser.get("platform") != "macOS"
        or not _nonblank(browser.get("version"))
    ):
        raise RuntimeError("explicit native Safari identity is missing or invalid")
    if not isinstance(configured, Mapping) or set(configured) != {
        "node_id",
        "incumbent",
        "instruction",
        "mode",
        "tolerance_pct",
        "max_cases",
        "needs_tools",
        "needs_vision",
        "judge_model",
        "max_candidates",
        "min_cases",
    }:
        raise RuntimeError("Rightsizing configured fields are incomplete")
    if (
        not all(
            _nonblank(configured.get(field))
            for field in ("node_id", "incumbent", "instruction", "judge_model")
        )
        or configured.get("mode") not in {"equivalence", "correctness"}
        or configured.get("tolerance_pct") != "101"
        or not _whole_number(configured.get("max_cases"), minimum=1, maximum=25)
        or not _whole_number(configured.get("max_candidates"), minimum=1, maximum=6)
        or not _whole_number(configured.get("min_cases"), minimum=1, maximum=50)
        or not isinstance(configured.get("needs_tools"), bool)
        or not isinstance(configured.get("needs_vision"), bool)
    ):
        raise RuntimeError("Rightsizing configured fields are invalid")
    if (
        not isinstance(submission, Mapping)
        or submission.get("attempted") is not True
        or submission.get("blocked_by") != "client_validation"
        or submission.get("measured_endpoint_request_observed") is not False
        or submission.get("error") != EXACT_ERROR
        or submission.get("invalid_control") != "rightsizing.experiment.tolerance-pct"
        or submission.get("aria_invalid") is not True
    ):
        raise RuntimeError("exact Rightsizing client validation result is missing")
    return {
        "schema_version": 1,
        "browser": dict(browser),
        "route": value["route"],
        "configured_fields": dict(configured),
        "submission": dict(submission),
    }


def _validate_sha(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RuntimeError(f"invalid {label}")


def _money(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise RuntimeError(f"invalid {label}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"invalid {label}")
    return result


def _count(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"invalid {label}")
    return value


def _validate_snapshot(value: Mapping[str, Any], *, label: str) -> dict[str, object]:
    planes = value.get("planes")
    if (
        value.get("schema_version") != 1
        or value.get("tenant_id") != TENANT
        or not _nonblank(value.get("captured_at"))
        or not isinstance(planes, Mapping)
        or set(planes) != {"measured_endpoint", "provider", "runs", "audits", "economics"}
    ):
        raise RuntimeError(f"invalid {label} snapshot")
    endpoint = planes.get("measured_endpoint")
    provider = planes.get("provider")
    runs = planes.get("runs")
    audits = planes.get("audits")
    economics = planes.get("economics")
    if not all(isinstance(item, Mapping) for item in (endpoint, provider, runs, audits, economics)):
        raise RuntimeError(f"invalid {label} snapshot planes")
    assert isinstance(endpoint, Mapping)
    assert isinstance(provider, Mapping)
    assert isinstance(runs, Mapping)
    assert isinstance(audits, Mapping)
    assert isinstance(economics, Mapping)
    if set(endpoint) != {"request_count", "last_request_id_sha256"}:
        raise RuntimeError(f"invalid {label} measured endpoint snapshot")
    if set(provider) != {"call_count", "request_ids_sha256"}:
        raise RuntimeError(f"invalid {label} provider snapshot")
    if set(runs) != {"count", "ids_sha256"}:
        raise RuntimeError(f"invalid {label} run snapshot")
    if set(audits) != {"count", "head_digest"}:
        raise RuntimeError(f"invalid {label} audit snapshot")
    if set(economics) != {
        "cost_event_count",
        "total_cost_usd",
        "reservation_count",
        "held_cost_usd",
    }:
        raise RuntimeError(f"invalid {label} economics snapshot")
    _count(endpoint.get("request_count"), label=f"{label} endpoint count")
    _validate_sha(endpoint.get("last_request_id_sha256"), label=f"{label} endpoint digest")
    _count(provider.get("call_count"), label=f"{label} provider count")
    _validate_sha(provider.get("request_ids_sha256"), label=f"{label} provider digest")
    _count(runs.get("count"), label=f"{label} run count")
    _validate_sha(runs.get("ids_sha256"), label=f"{label} run digest")
    _count(audits.get("count"), label=f"{label} audit count")
    _validate_sha(audits.get("head_digest"), label=f"{label} audit digest")
    _count(economics.get("cost_event_count"), label=f"{label} cost event count")
    _money(economics.get("total_cost_usd"), label=f"{label} total cost")
    _count(economics.get("reservation_count"), label=f"{label} reservation count")
    _money(economics.get("held_cost_usd"), label=f"{label} held cost")
    return {
        "schema_version": 1,
        "tenant_id": TENANT,
        "captured_at": value["captured_at"],
        "planes": {name: dict(plane) for name, plane in planes.items()},
    }


def _zero_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, object]:
    before_planes = before["planes"]
    after_planes = after["planes"]
    assert isinstance(before_planes, Mapping)
    assert isinstance(after_planes, Mapping)
    if before_planes != after_planes:
        raise RuntimeError("Rightsizing boundary submission has a non-zero side-effect delta")
    economics = before_planes["economics"]
    assert isinstance(economics, Mapping)
    return {
        "measured_endpoint_request_delta": 0,
        "provider_call_delta": 0,
        "run_delta": 0,
        "audit_delta": 0,
        "cost_event_delta": 0,
        "reservation_delta": 0,
        "total_cost_usd_delta": format(
            _money(economics["total_cost_usd"], label="total cost")
            - _money(economics["total_cost_usd"], label="total cost"),
            ".8f",
        ),
    }


def _native_ax_value(text: str, label_pattern: str, value: object) -> bool:
    return bool(
        re.search(
            rf"(?m)^\s*\d+\s+{label_pattern}(?=\s|,)[^\n]*,\s*Value:\s*"
            rf"{re.escape(str(value))}(?:,|\s*$)",
            text,
        )
    )


def _validate_native_ax(text: str, observation: Mapping[str, Any], *, error: bool) -> None:
    configured = observation["configured_fields"]
    assert isinstance(configured, Mapping)
    if 'Window: "Zeroth Console", App: Safari.' not in text or (
        "URL: 127.0.0.1:3000/console/rightsizing/" not in text
    ):
        raise RuntimeError("raw native Safari accessibility identity is missing")
    if "rightsizing.experiment." in text or 'browser="Safari"' in text:
        raise RuntimeError("normalized accessibility projection is not native Safari AX output")
    equivalence = 1 if configured["mode"] == "equivalence" else 0
    correctness = 1 - equivalence
    values = (
        (r"text field \(settable\) node_id", configured["node_id"]),
        (r"text field \(settable\) incumbent", configured["incumbent"]),
        (r"text entry area \(settable\) instruction", configured["instruction"]),
        (r"radio button vs\. incumbent", equivalence),
        (r"radio button vs\. correct answer", correctness),
        (r"text field \(settable\) Tolerance \(%\)", 101),
        (r"text field \(settable\) Maximum cases", configured["max_cases"]),
        (
            r"checkbox Candidate needs tools",
            int(bool(configured["needs_tools"])),
        ),
        (
            r"checkbox Candidate needs vision",
            int(bool(configured["needs_vision"])),
        ),
        (r"text field \(settable\) Judge model", configured["judge_model"]),
        (
            r"text field \(settable\) Maximum candidates",
            configured["max_candidates"],
        ),
        (r"text field \(settable\) Minimum cases", configured["min_cases"]),
    )
    if any(not _native_ax_value(text, label, value) for label, value in values):
        raise RuntimeError("native Safari accessibility field values are incomplete")
    if re.search(r"(?m)^\s*\d+\s+button \(disabled\) Run experiment\s*$", text) or not re.search(
        r"(?m)^\s*\d+\s+button Run experiment\s*$", text
    ):
        raise RuntimeError("native Safari accessibility Run experiment button is not enabled")
    error_present = bool(re.search(rf"(?m)^\s*\d+\s+text {re.escape(EXACT_ERROR)}\s*$", text))
    if error_present is not error:
        raise RuntimeError("native Safari accessibility validation error state drifted")


def _validate_artifacts(source_root: Path, observation: Mapping[str, Any]) -> None:
    expected = {
        *(f"screenshots/{name}" for name in SCREENSHOTS),
        *(f"accessibility/{name}" for name in ACCESSIBILITY),
        *(f"runtime/{name}" for name in RUNTIME),
    }
    actual = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise RuntimeError("native Safari Rightsizing staging inventory is not exact")
    for name in SCREENSHOTS:
        path = source_root / "screenshots" / name
        payload = path.read_bytes()
        if path.is_symlink() or len(payload) < 256 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("native Safari Rightsizing PNG screenshot is invalid")
    for index, name in enumerate(ACCESSIBILITY):
        path = source_root / "accessibility" / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("native Safari Rightsizing accessibility evidence is missing")
        text = path.read_text(encoding="utf-8")
        try:
            _validate_native_ax(text, observation, error=index == 1)
        except RuntimeError as exc:
            raise RuntimeError("native Safari Rightsizing accessibility evidence drifted") from exc


def build_checkpoint(*, source_root: Path, destination: Path, repository_root: Path) -> Path:
    """Validate immutable Safari boundary evidence and seal an accepted bundle."""
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    repository_root = repository_root.expanduser().resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    if source_root == destination or source_root in destination.parents:
        raise ValueError("evidence destination must not be inside staging")
    EvidenceStore(source_root).scan_recursive()
    observation = _validate_observation(
        _load_object(source_root / "runtime/observation.json", label="UI observation")
    )
    _validate_artifacts(source_root, observation)
    before = _validate_snapshot(
        _load_object(source_root / "runtime/before.json", label="before snapshot"),
        label="before",
    )
    after = _validate_snapshot(
        _load_object(source_root / "runtime/after.json", label="after snapshot"),
        label="after",
    )
    delta = _zero_delta(before, after)
    health = _load_object(source_root / "runtime/health.json", label="D-012 health")
    if health != EXPECTED_D012:
        raise RuntimeError("current D-012 health is not exact")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("repository revision is invalid")

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "native-safari-rightsizing-boundary-submit",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": TENANT,
            "revision": revision,
            "diff_sha256": dirty_tree_hash(repository_root).removeprefix("sha256:"),
            "source_root": str(source_root),
            "browser": observation["browser"],
            "route": observation["route"],
            "validation_error": EXACT_ERROR,
            "side_effect_delta": delta,
            "served_identity": health,
            "provider_calls_performed": 0,
        }
    )
    for name, value in (
        ("observation.json", observation),
        ("before.json", before),
        ("after.json", after),
        ("health.json", health),
        ("delta.json", delta),
    ):
        store._write_exclusive(Path("runtime") / name, value)
    evidence = [f"runtime/{name}" for name in (*RUNTIME, "delta.json")]
    for folder, names in (
        ("screenshots", SCREENSHOTS),
        ("accessibility", ACCESSIBILITY),
    ):
        for name in names:
            relative = f"{folder}/{name}"
            store.ingest_artifact(source_root / relative, relative)
            evidence.append(relative)
    event_id = store.append_event(
        "campaign.ui.rightsizing_boundary_verified",
        {
            "result": "pass",
            "route": "/console/rightsizing/",
            "invalid_value": 101,
            "validation_error": EXACT_ERROR,
            "measured_endpoint_request_delta": 0,
            "provider_call_delta": 0,
            "run_delta": 0,
            "audit_delta": 0,
            "cost_event_delta": 0,
            "reservation_delta": 0,
        },
        correlation=CorrelationIds(ui_action_id="native-safari-rightsizing-tolerance-101-submit"),
    )
    references = tuple([*evidence, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", references) for criterion in CRITERIA
        ),
        report_markdown=(
            "# Native Safari Rightsizing boundary-submit checkpoint\n\n"
            "Native Safari displayed the connected Rightsizing experiment form with every "
            "required field configured. Submitting tolerance `101` produced the exact client "
            f"validation message: `{EXACT_ERROR}` The control exposed `aria-invalid=true`. "
            "The measured-experiment request counter, provider-call inventory, normal runs, "
            "signed audits, cost events, reservations, held exposure, and total cost were "
            "identical before and after submission. Current health remained pinned to the exact "
            "frozen D-012 deployment and graph. This checkpoint proves a client-side rejection; "
            "it does not claim that the measured provider endpoint executed.\n"
        ),
    )
    store.scan_recursive()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
        repository_root=args.repository_root,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
