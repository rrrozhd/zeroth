"""Service-layer client for producing a headless economic diagnostic artifact."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from zeroth.integrations.http.factory import aclose_all, governed_async_client

_STATEMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_MAX_USD_AMOUNT = Decimal("10000000000")
_MAX_MIGRATION_JSON_BYTES = 1024 * 1024
_MAX_MIGRATION_OUTPUT_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 20 * 1024 * 1024
_MIGRATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]+$")
_MAX_BASE_URL_BYTES = 2048
_MAX_TOKEN_BYTES = 8192


class _MigrationLocalError(ValueError):
    pass


class _MigrationRemoteError(RuntimeError):
    def __init__(self, status: int | None, request_id: str | None = None) -> None:
        self.status = status
        self.request_id = request_id


def _money(value: object) -> str:
    """Render an optional numeric value as a fixed-precision USD amount."""
    return "—" if value is None else f"${float(value):.8f}"


def render_markdown(report: dict[str, Any]) -> str:
    """Render the API's bounded claims without inventing savings."""
    workflow_id = str(report["workflow_id"])
    action = report["recommended_action"]
    top = report.get("top_failure_exposure")
    cohort = report.get("highest_failure_rate_cohort")
    undefined_versions = report.get("undefined_outcome_versions") or []
    lines = [
        f"# Zeroth economic diagnostic: {workflow_id}",
        "",
        f"**Decision state:** {str(report['decision_state']).replace('_', ' ')}",
        f"**Evidence quality:** {str(report['data_quality']).replace('_', ' ')}",
        f"**Claim scope:** {str(report['claim_scope']).replace('_', ' ')}",
        f"**Method version:** `{report.get('method_version', 'legacy_unversioned')}`",
    ]
    if undefined_versions:
        lines.append(
            "**Undefined outcome versions:** "
            + ", ".join(f"`{version}`" for version in undefined_versions)
        )
    lines.extend(
        [
            "",
            "## Economics",
            "",
            "| Metric | Observed value |",
            "|---|---:|",
            f"| Runs | {report['runs']} |",
            f"| Successful runs | {report['successful_runs']} |",
            f"| Failed runs | {report['failed_runs']} |",
            f"| Unresolved runs | {report['unresolved_runs']} |",
            f"| Outcome coverage | {float(report['outcome_coverage']):.1%} |",
            f"| Measured cost | {_money(report['measured_cost_usd'])} |",
            f"| Estimated cost | {_money(report['estimated_cost_usd'])} |",
            (
                "| Measured cost per successful outcome | "
                f"{_money(report['measured_cost_per_successful_outcome_usd'])} |"
            ),
            (
                "| Measured failed-run exposure | "
                f"{_money(report['measured_failure_exposure_usd'])} |"
            ),
            (
                "| Estimated failed-run exposure | "
                f"{_money(report['estimated_failure_exposure_usd'])} |"
            ),
            "",
        ]
    )
    if top is not None:
        lines.extend(
            [
                "## Highest failed-run exposure",
                "",
                (
                    f"`{top['workflow_version']}` / `{top['step_id']}` contains "
                    f"{_money(top['measured_failure_exposure_usd'])} measured and "
                    f"{_money(top['estimated_failure_exposure_usd'])} estimated exposure "
                    f"across {top['failed_runs']} failed run(s)."
                ),
                "",
            ]
        )
    if cohort is not None:
        resolved = cohort["successful_runs"] + cohort["failed_runs"]
        failure_rate = cohort["failed_runs"] / resolved if resolved else 0.0
        lines.extend(
            [
                "## Highest-failure cohort",
                "",
                (
                    f"`{cohort['cohort']}` has a {failure_rate:.1%} failure rate over "
                    f"{resolved} resolved run(s)."
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## Recommended next action",
            "",
            f"**{str(action['code']).replace('_', ' ')}.** {action['rationale']}",
            "",
            action["supported_claim"],
            "",
            "## Claim limits",
            "",
            *(f"- {limitation}" for limitation in report["limitations"]),
            "",
        ]
    )
    return "\n".join(lines)


def render_bill_markdown(report: dict[str, Any]) -> str:
    """Render financial closure without hiding allocation or outcome variance."""
    lines = [
        f"# Zeroth provider bill: {report['provider']} / {report['statement_id']}",
        "",
        (f"**Reconciliation state:** {str(report['reconciliation_state']).replace('_', ' ')}"),
        f"**Statement digest:** `{report['statement_digest']}`",
        f"**Allocation method:** {str(report['allocation_method']).replace('_', ' ')}",
        f"**Method version:** `{report.get('method_version', 'legacy_unversioned')}`",
        "",
        "## Closure",
        "",
        "| Metric | Amount |",
        "|---|---:|",
        f"| Provider billed total | {_money(report['billed_total_usd'])} |",
        f"| Allocated billed dollars | {_money(report['allocated_billed_usd'])} |",
        f"| Unreconciled billed dollars | {_money(report['unreconciled_billed_usd'])} |",
        f"| Measured telemetry | {_money(report['telemetry_measured_usd'])} |",
        f"| Telemetry variance | {_money(report['telemetry_variance_usd'])} |",
        f"| Unbilled telemetry | {_money(report['unbilled_telemetry_usd'])} |",
        f"| Outcome-unresolved dollars | {_money(report['outcome_unresolved_usd'])} |",
        "",
        "## Workflow and outcome allocation",
        "",
        "| Provider bucket | Workflow | Version | Outcome | Billed | Telemetry | Runs | Events |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report["allocations"]:
        lines.append(
            f"| {row['bucket_id']} | {row['workflow_id']} | {row['workflow_version']} | "
            f"{row['outcome_status']} | {_money(row['billed_cost_usd'])} | "
            f"{_money(row['telemetry_cost_usd'])} | {row['run_count']} | "
            f"{row['event_count']} |"
        )
    if report["unmatched_buckets"]:
        lines.extend(["", "## Unmatched provider buckets", ""])
        lines.extend(
            f"- `{row['bucket_id']}`: {str(row['reason']).replace('_', ' ')}"
            for row in report["unmatched_buckets"]
        )
    lines.extend(
        [
            "",
            "## Claim limits",
            "",
            *(f"- {limitation}" for limitation in report["limitations"]),
            "",
        ]
    )
    return "\n".join(lines)


def _utc_timestamp(value: int) -> str:
    """Convert a Unix timestamp to the canonical UTC representation used in imports."""
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    """Require an object-shaped JSON field and return its typed mapping."""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: object, *, field: str) -> list[Any]:
    """Require an array-shaped JSON field and return its typed list."""
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _openai_cost_amount(value: object, *, field: str) -> Decimal:
    """Validate one OpenAI USD cost against Zeroth's precision and range limits."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{field} must be a JSON number")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be non-negative")
    if amount.normalize().as_tuple().exponent < -8:
        raise ValueError(f"{field} exceeds Zeroth's 8-decimal precision")
    if amount >= _MAX_USD_AMOUNT:
        raise ValueError(f"{field} exceeds Zeroth's 10 whole-number digits")
    return amount


def _normalize_openai_costs_page(page: dict[str, Any], *, statement_id: str) -> dict[str, Any]:
    """Convert one complete OpenAI Costs page into a bounded Zeroth statement."""
    if _STATEMENT_ID.fullmatch(statement_id) is None:
        raise ValueError("statement_id is not URL-safe")
    if page.get("object") != "page":
        raise ValueError("input is not an OpenAI Costs API page")
    if page.get("has_more") is not False or page.get("next_page") is not None:
        raise ValueError(
            "input is an incomplete page; fetch all requested days in one final Costs API page"
        )
    data = _list(page.get("data"), field="data")
    grouped: defaultdict[tuple[int, int, str | None], Decimal] = defaultdict(Decimal)
    project_scopes: set[bool] = set()
    for bucket_index, raw_bucket in enumerate(data):
        bucket = _mapping(raw_bucket, field=f"data[{bucket_index}]")
        if bucket.get("object") != "bucket":
            raise ValueError(f"data[{bucket_index}] is not an OpenAI cost bucket")
        start = bucket.get("start_time")
        end = bucket.get("end_time")
        if type(start) is not int or type(end) is not int or start >= end:
            raise ValueError(f"data[{bucket_index}] has an invalid time interval")
        results = _list(bucket.get("results"), field=f"data[{bucket_index}].results")
        for result_index, raw_result in enumerate(results):
            field = f"data[{bucket_index}].results[{result_index}]"
            result = _mapping(raw_result, field=field)
            if result.get("object") != "organization.costs.result":
                raise ValueError(f"{field} is not an OpenAI costs result")
            amount = _mapping(result.get("amount"), field=f"{field}.amount")
            currency = amount.get("currency")
            if not isinstance(currency, str) or currency.lower() != "usd":
                raise ValueError(f"{field}.amount.currency must be USD")
            value = _openai_cost_amount(amount.get("value"), field=f"{field}.amount.value")
            project_id = result.get("project_id")
            if project_id is not None and (
                not isinstance(project_id, str) or not project_id or len(project_id) > 256
            ):
                raise ValueError(f"{field}.project_id must be null or 1-256 characters")
            line_item = result.get("line_item")
            if line_item is not None and not isinstance(line_item, str):
                raise ValueError(f"{field}.line_item must be null or a string")
            project_scopes.add(project_id is not None)
            if value > 0:
                grouped[(start, end, project_id)] += value
    if project_scopes == {False, True}:
        raise ValueError("OpenAI response mixes project-scoped and unscoped cost results")
    if not grouped:
        raise ValueError("OpenAI response contains no positive USD cost")
    if len(grouped) > 10_000:
        raise ValueError("OpenAI response exceeds Zeroth's 10,000-bucket limit")
    if any(amount >= _MAX_USD_AMOUNT for amount in grouped.values()):
        raise ValueError("an aggregated OpenAI bucket exceeds Zeroth's USD range")

    ordered = sorted(grouped.items(), key=lambda item: item[0])
    last_end_by_project: dict[str | None, int] = {}
    buckets: list[dict[str, Any]] = []
    for (start, end, project_id), amount in ordered:
        if start < last_end_by_project.get(project_id, start):
            raise ValueError("OpenAI response contains overlapping time buckets")
        last_end_by_project[project_id] = end
        scope_digest = sha256((project_id or "unscoped").encode()).hexdigest()[:12]
        buckets.append(
            {
                "bucket_id": f"openai:{start}:{end}:{scope_digest}",
                "period_start": _utc_timestamp(start),
                "period_end": _utc_timestamp(end),
                "amount_usd": format(amount, "f"),
                "model": None,
                "provider_dimensions": (
                    {"project_id": project_id} if project_id is not None else {}
                ),
            }
        )
    total = sum(grouped.values(), Decimal("0"))
    if total >= _MAX_USD_AMOUNT:
        raise ValueError("the OpenAI statement total exceeds Zeroth's USD range")
    return {
        "statement_id": statement_id,
        "provider": "openai",
        "period_start": _utc_timestamp(min(key[0] for key in grouped)),
        "period_end": _utc_timestamp(max(key[1] for key in grouped)),
        "currency": "USD",
        "billed_total_usd": format(total, "f"),
        "source_kind": "cost_api",
        "buckets": buckets,
    }


async def _normalize_openai_costs(args: argparse.Namespace) -> int:
    """Normalize an offline OpenAI Costs export and write the requested artifact."""
    try:
        page = json.loads(
            Path(args.input).read_text(encoding="utf-8"),
            parse_float=Decimal,
        )
        statement = _normalize_openai_costs_page(
            _mapping(page, field="input"), statement_id=args.statement_id
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"normalize-openai-costs failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(statement, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(rendered, end="")
    else:
        output = Path(args.output).resolve()
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    return 0


async def _diagnose(args: argparse.Namespace) -> int:
    """Fetch one authenticated workflow diagnostic and render it for the operator."""
    token = os.getenv(args.token_env)
    if not token:
        print(f"diagnose failed: set {args.token_env} to an econ-plane JWT", file=sys.stderr)
        return 2
    params: dict[str, str] = {"workflow_id": args.workflow_id}
    if args.start:
        params["start"] = args.start
    if args.end:
        params["end"] = args.end
    if args.cohort_dimension:
        params["cohort_dimension"] = args.cohort_dimension
    base_url = f"{args.base_url.rstrip('/')}/"
    try:
        client = await governed_async_client(
            purpose="economic-diagnostic-cli",
            timeout=args.timeout,
            base_url=base_url,
        )
        response = await client.get(
            "debugger/report",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        report = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"diagnose failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await aclose_all()
    if not isinstance(report, dict):
        print("diagnose failed: server returned a non-object report", file=sys.stderr)
        return 1

    rendered = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )
    if args.output == "-":
        print(rendered, end="")
    else:
        output = Path(args.output).resolve()
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    return 0


async def _reconcile(args: argparse.Namespace) -> int:
    """Import a normalized bill and render its authenticated closure report."""
    token = os.getenv(args.token_env)
    if not token:
        print(f"reconcile failed: set {args.token_env} to an econ-plane JWT", file=sys.stderr)
        return 2
    try:
        statement = json.loads(Path(args.statement).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reconcile failed: cannot read statement: {exc}", file=sys.stderr)
        return 2
    if not isinstance(statement, dict):
        print("reconcile failed: statement must be a JSON object", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {token}"}
    base_url = f"{args.base_url.rstrip('/')}/"
    try:
        client = await governed_async_client(
            purpose="provider-bill-reconciliation-cli",
            timeout=args.timeout,
            base_url=base_url,
        )
        imported_response = await client.post(
            "reconciliation/provider-bills",
            json=statement,
            headers=headers,
        )
        imported_response.raise_for_status()
        imported = imported_response.json()
        if not isinstance(imported, dict):
            raise ValueError("server returned a non-object import result")
        provider = imported.get("provider")
        statement_id = imported.get("statement_id")
        if not isinstance(provider, str) or not isinstance(statement_id, str):
            raise ValueError("server import result omitted provider or statement_id")
        report_response = await client.get(
            "reconciliation/provider-bills/"
            f"{quote(provider, safe='')}/{quote(statement_id, safe='')}/report",
            headers=headers,
        )
        report_response.raise_for_status()
        report = report_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"reconcile failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await aclose_all()
    if not isinstance(report, dict):
        print("reconcile failed: server returned a non-object report", file=sys.stderr)
        return 1
    rendered = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_bill_markdown(report)
    )
    if args.output == "-":
        print(rendered, end="")
    else:
        output = Path(args.output).resolve()
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    return 0


async def _demo(args: argparse.Namespace) -> int:
    """Generate the explicitly synthetic local economic-debugger evidence pack."""
    try:
        from zeroth.service.economic_demo import generate_demo_pack
    except ModuleNotFoundError as exc:
        print(
            "demo failed: install the economic service dependencies with "
            '`pip install "zeroth-platform[regulus]"` '
            f"({exc})",
            file=sys.stderr,
        )
        return 2
    try:
        output = generate_demo_pack(
            Path(args.output_dir),
            render_diagnostic=render_markdown,
            render_reconciliation=render_bill_markdown,
        )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 2
    print(f"Synthetic example — not customer evidence\nWrote {output}")
    return 0


def _unavailable(value: object, *, suffix: str = "") -> str:
    return "unavailable" if value is None else f"{value}{suffix}"


def render_migration_markdown(value: object) -> str:
    """Render recorded migration diagnostics without implying validated forecasts."""
    if isinstance(value, list):
        if not value:
            return "# Migration decision history\n\nNo retained decisions.\n"
        return "\n---\n\n".join(render_migration_markdown(item) for item in value)
    if not isinstance(value, dict):
        return "# Migration operation\n\nResult unavailable.\n"
    if "verdict" not in value:
        return (
            "# Migration operation\n\n**Advisory-only:** no production routing was changed.\n\n"
            "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```\n"
        )
    verdict = str(value.get("verdict") or "unavailable").upper()
    action = str(value.get("recommended_action") or "unavailable").replace("_", " ")
    reasons = value.get("reason_codes") if isinstance(value.get("reason_codes"), list) else []
    readiness = value.get("forecast_readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    lineage = value.get("evidence_lineage")
    lineage = lineage if isinstance(lineage, dict) else {}
    routing = value.get("recommended_routing")
    routing = routing if isinstance(routing, dict) else {}
    selected = None
    for candidate in value.get("actions") or []:
        if isinstance(candidate, dict) and candidate.get("candidate_share") == value.get(
            "recommended_candidate_share"
        ):
            selected = candidate
            break
    selected = selected or {}
    feasible = selected.get("feasible")
    qualification = (
        "feasible" if feasible is True else "infeasible" if feasible is False else "unavailable"
    )
    lines = [
        f"# Recorded migration decision: {verdict}",
        "",
        "Experimental scenario diagnostics. Predictive reliability is unvalidated. "
        "Simulated savings and risk estimates do not establish future outcomes "
        "or authorize rollout. "
        "Recorded actions and verdicts are retained for audit.",
        "",
        f"**Recorded action:** {action}",
        "**Advisory-only:** this result does not change production routing.",
        "**Additional evidence required:** "
        + _unavailable(value.get("additional_cases_required"), suffix=" cases"),
        f"**Reason codes:** {', '.join(map(str, reasons)) if reasons else 'unavailable'}",
        "",
        "## Recorded proposed route",
        "",
        f"**Candidate traffic share:** {_unavailable(value.get('recommended_candidate_share'))}",
        "**Complete cohort route:** "
        + (json.dumps(routing, sort_keys=True) if routing else "unavailable"),
        "",
        "## Recorded forecast diagnostics",
        "",
        f"**Recorded calibration state:** {_unavailable(readiness.get('calibration_state'))}",
        f"**Recorded drift state:** {_unavailable(readiness.get('drift_state'))}",
        "",
        "## Simulated risk and economics",
        "",
        f"- Simulated monthly cost: {_unavailable(selected.get('expected_monthly_cost_usd'))}",
        "- Simulated savings p05-p95: "
        + _unavailable(selected.get("monthly_savings_p05_usd"))
        + " to "
        + _unavailable(selected.get("monthly_savings_p95_usd")),
        f"- Quality breach probability: {_unavailable(selected.get('probability_quality_breach'))}",
        f"- Latency breach probability: {_unavailable(selected.get('probability_latency_breach'))}",
        "- Critical-error breach probability: "
        + _unavailable(selected.get("probability_critical_error_breach")),
        f"- CVaR loss: {_unavailable(selected.get('cvar_loss_usd'))}",
        f"- Quality-drop tolerance: {_unavailable(selected.get('minimum_quality_drop_tolerance'))}",
        f"- P95 latency limit: {_unavailable(selected.get('minimum_p95_latency_limit_ms'))}",
        "- Critical-error-rate limit: "
        + _unavailable(selected.get("minimum_critical_error_rate_limit")),
        f"- CVaR loss limit: {_unavailable(selected.get('minimum_cvar_loss_limit_usd'))}",
        f"- Recorded feasibility: {qualification}",
        f"- Simulations: {_unavailable(value.get('simulations'))}",
        "",
        "## Identifiers",
        "",
        f"- Decision: {_unavailable(value.get('decision_id'))}",
        f"- Algorithm: {_unavailable(lineage.get('algorithm_version'))}",
        f"- Evidence: {_unavailable(lineage.get('evidence_id'))}",
        "",
    ]
    return "\n".join(lines)


def _migration_identifier(value: str, *, field: str) -> str:
    if _MIGRATION_ID.fullmatch(value) is None:
        raise _MigrationLocalError(f"{field} must be a safe 1-192 character identifier")
    return quote(value, safe="")


def _remote_migration_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise _MigrationRemoteError(None)
    try:
        return _migration_identifier(value, field=field)
    except _MigrationLocalError as exc:
        raise _MigrationRemoteError(None) from exc


def _migration_input(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise _MigrationLocalError("input must be a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise _MigrationLocalError("input must be a direct regular file")
        if opened.st_size > _MAX_MIGRATION_JSON_BYTES:
            raise _MigrationLocalError("input is too large")
        chunks: list[bytes] = []
        remaining = _MAX_MIGRATION_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_MIGRATION_JSON_BYTES:
            raise _MigrationLocalError("input is too large")
    except UnicodeDecodeError as exc:
        raise _MigrationLocalError("input must be UTF-8 JSON") from exc
    except OSError as exc:
        raise _MigrationLocalError("input could not be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise _MigrationLocalError("input must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise _MigrationLocalError("input must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise _MigrationLocalError("input JSON must be an object")
    return payload


def _migration_config(args: argparse.Namespace) -> tuple[str, str]:
    if _TOKEN_ENV_NAME.fullmatch(args.token_env) is None:
        raise _MigrationLocalError("token environment name is invalid")
    token = os.getenv(args.token_env)
    if not token:
        raise _MigrationLocalError(f"set {args.token_env} to an econ-plane JWT")
    if len(token.encode("utf-8")) > _MAX_TOKEN_BYTES or _VISIBLE_ASCII.fullmatch(token) is None:
        raise _MigrationLocalError("configured token is invalid")
    if not 0 < args.timeout <= 300:
        raise _MigrationLocalError("timeout must be between 0 and 300 seconds")
    if (
        len(args.base_url.encode("utf-8")) > _MAX_BASE_URL_BYTES
        or _VISIBLE_ASCII.fullmatch(args.base_url) is None
    ):
        raise _MigrationLocalError("base URL is invalid")
    try:
        parsed = urlsplit(args.base_url)
        _ = parsed.port
        _ = httpx.URL(args.base_url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise _MigrationLocalError("base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _MigrationLocalError("base URL must be an HTTP(S) origin/path without credentials")
    return token, f"{args.base_url.rstrip('/')}/"


def _operator_text(value: str, *, field: str, max_bytes: int) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > max_bytes
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise _MigrationLocalError(f"{field} is invalid")
    return value


async def _bounded_remote_content(
    client: Any,
    method: str,
    path: str,
    *,
    cap: int,
    **kwargs: object,
) -> bytes:
    async with client.stream(method, path, **kwargs) as response:
        status = int(response.status_code)
        if not 200 <= status < 300:
            request_id = response.headers.get("x-request-id")
            if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
                request_id = None
            raise _MigrationRemoteError(status, request_id)
        content_encoding = response.headers.get("content-encoding")
        if content_encoding is not None and content_encoding.strip().lower() != "identity":
            raise _MigrationRemoteError(None)
        declared = response.headers.get("content-length")
        if declared is not None and (
            len(declared) > 20 or re.fullmatch(r"[0-9]+", declared) is None or int(declared) > cap
        ):
            raise _MigrationRemoteError(None)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > cap:
                raise _MigrationRemoteError(None)
            chunks.append(chunk)
        return b"".join(chunks)


async def _migration_json_request(client: Any, method: str, path: str, **kwargs: object) -> object:
    content = await _bounded_remote_content(
        client, method, path, cap=_MAX_MIGRATION_JSON_BYTES, **kwargs
    )
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _MigrationRemoteError(None) from exc
    if not isinstance(payload, (dict, list)):
        raise _MigrationRemoteError(None)
    return payload


def _write_migration_output(value: object, args: argparse.Namespace) -> None:
    rendered = (
        json.dumps(value, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_migration_markdown(value)
    )
    if len(rendered.encode("utf-8")) > _MAX_MIGRATION_OUTPUT_BYTES:
        raise _MigrationRemoteError(None)
    if args.output == "-":
        print(rendered, end="")
        return
    _publish_exclusive(Path(args.output), rendered.encode("utf-8"))


def _publish_exclusive(output: Path, payload: bytes) -> None:
    """Atomically publish bounded bytes without replacing an existing path."""
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            written = handle.write(payload)
            if written != len(payload):
                raise OSError("short write")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    except FileExistsError as exc:
        raise _MigrationLocalError("output path already exists") from exc
    except OSError as exc:
        raise _MigrationLocalError("output could not be written without overwrite") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


async def _migration(args: argparse.Namespace) -> int:
    """Execute one bounded authenticated migration operation through the public API."""
    client = None
    try:
        token, base_url = _migration_config(args)
        command = args.migration_command
        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
        body = _migration_input(args.input) if hasattr(args, "input") else None
        paths = {
            "evaluate": ("POST", "decisions/model-migration"),
            "refresh": ("POST", "decisions/model-migration/refresh"),
            "schedule-create": ("POST", "probabilistic-decision-schedules"),
            "rollout-create": ("POST", "randomized-rollouts"),
        }
        if command == "schedule-deactivate":
            schedule_id = _migration_identifier(args.schedule_id, field="schedule ID")
            paths[command] = (
                "POST",
                f"probabilistic-decision-schedules/{schedule_id}/deactivate",
            )
        if command in {"rollout-assign", "rollout-verify", "rollout-stop"}:
            suffix = {
                "rollout-assign": "assignments",
                "rollout-verify": "verify",
                "rollout-stop": "stop",
            }[command]
            rollout_id = _migration_identifier(args.rollout_id, field="rollout ID")
            paths[command] = (
                "POST",
                f"randomized-rollouts/{rollout_id}/{suffix}",
            )
        if command == "report":
            pdf_output = Path(args.output)
            if pdf_output.exists():
                raise _MigrationLocalError("output path already exists")
            if not pdf_output.parent.is_dir():
                raise _MigrationLocalError("output parent directory does not exist")
            if len(args.recipient or []) > 20:
                raise _MigrationLocalError("at most 20 recipients are allowed")
            recipients = [
                _operator_text(value, field="recipient", max_bytes=320)
                for value in (args.recipient or [])
            ]
        if command == "history" and args.workload is not None:
            workload = _operator_text(args.workload, field="workload", max_bytes=192)
        client = await governed_async_client(
            purpose="economic-migration-cli", timeout=args.timeout, base_url=base_url
        )
        if command == "history":
            params = {"limit": args.limit}
            if args.workload:
                params["workload"] = workload
            result = await _migration_json_request(
                client, "GET", "decisions/model-migrations", headers=headers, params=params
            )
            _write_migration_output(result, args)
            return 0
        if command == "report":
            decision_id = _migration_identifier(args.decision_id, field="decision ID")
            report = await _migration_json_request(
                client, "POST", f"decisions/{decision_id}/reports", headers=headers, json={}
            )
            if not isinstance(report, dict):
                raise _MigrationRemoteError(200)
            report_id = _remote_migration_identifier(report.get("report_id"), field="report ID")
            pdf = await _bounded_remote_content(
                client,
                "GET",
                f"reports/{report_id}",
                cap=_MAX_REPORT_BYTES,
                headers=headers,
            )
            _publish_exclusive(Path(args.output), pdf)
            delivery = None
            if recipients:
                delivery = await _migration_json_request(
                    client,
                    "POST",
                    f"reports/{report_id}/deliveries",
                    headers=headers,
                    json={"recipients": recipients, "delivery_mode": args.delivery_mode},
                )
            summary = {"report": report, "delivery": delivery}
            rendered = (
                json.dumps(summary, indent=2, sort_keys=True) + "\n"
                if args.format == "json"
                else render_migration_markdown(summary)
            )
            if len(rendered.encode("utf-8")) > _MAX_MIGRATION_OUTPUT_BYTES:
                raise _MigrationRemoteError(None)
            print(rendered, end="")
            return 0
        method, path = paths[command]
        kwargs: dict[str, object] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        result = await _migration_json_request(client, method, path, **kwargs)
        _write_migration_output(result, args)
        return 0
    except _MigrationLocalError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    except _MigrationRemoteError as exc:
        if exc.status is None:
            print("migration failed: invalid remote response", file=sys.stderr)
            return 1
        guidance = {
            401: "authentication rejected",
            402: "entitlement required",
            403: "role denied",
            404: "resource unavailable",
            409: "operation conflicted",
            422: "request rejected",
            429: "rate limited",
        }.get(exc.status, "remote service failed")
        request = f" request_id={exc.request_id}" if exc.request_id else ""
        print(f"migration failed: HTTP {exc.status} {guidance}{request}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("migration failed: transport unavailable", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            await aclose_all()


def _add_migration_common(parser: argparse.ArgumentParser, *, output: bool = True) -> None:
    parser.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    parser.add_argument("--token-env", default="ZEROTH_ECON_TOKEN")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    if output:
        parser.add_argument("--output", default="-")


def _add_migration_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, help="bounded JSON request object")
    _add_migration_common(parser)


def build_parser() -> argparse.ArgumentParser:
    """Build the headless economic-debugger command-line interface."""
    parser = argparse.ArgumentParser(
        prog="zeroth-econ",
        description="Debug workflow economics and reconcile provider bills without the UI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    migration = subparsers.add_parser(
        "migration", help="operate the advisory model-migration decision loop"
    )
    migration_commands = migration.add_subparsers(dest="migration_command", required=True)
    for name in ("evaluate", "refresh", "schedule-create", "rollout-create"):
        command = migration_commands.add_parser(name)
        _add_migration_input(command)
        command.set_defaults(func=_migration)
    history = migration_commands.add_parser("history")
    history.add_argument("--workload", default=None)
    history.add_argument("--limit", type=int, choices=range(1, 201), default=50)
    _add_migration_common(history)
    history.set_defaults(func=_migration)
    report = migration_commands.add_parser("report")
    report.add_argument("--decision-id", required=True)
    report.add_argument("--output", required=True, help="new PDF output path")
    report.add_argument("--recipient", action="append", default=[])
    report.add_argument("--delivery-mode", choices=("attachment", "link"), default="link")
    _add_migration_common(report, output=False)
    report.set_defaults(func=_migration)
    schedule_deactivate = migration_commands.add_parser("schedule-deactivate")
    schedule_deactivate.add_argument("--schedule-id", required=True)
    _add_migration_common(schedule_deactivate)
    schedule_deactivate.set_defaults(func=_migration)
    for name in ("rollout-assign", "rollout-verify"):
        command = migration_commands.add_parser(name)
        command.add_argument("--rollout-id", required=True)
        _add_migration_input(command)
        command.set_defaults(func=_migration)
    rollout_stop = migration_commands.add_parser("rollout-stop")
    rollout_stop.add_argument("--rollout-id", required=True)
    _add_migration_common(rollout_stop)
    rollout_stop.set_defaults(func=_migration)
    demo = subparsers.add_parser(
        "demo", help="write a synthetic local diagnostic and provider-bill closure pack"
    )
    demo.add_argument("--output-dir", default="zeroth-economic-demo")
    demo.set_defaults(func=_demo)
    diagnose = subparsers.add_parser(
        "diagnose", help="fetch and render one workflow's economic diagnostic"
    )
    diagnose.add_argument("--workflow-id", required=True)
    diagnose.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    diagnose.add_argument("--token-env", default="ZEROTH_ECON_TOKEN")
    diagnose.add_argument("--start", default=None, help="inclusive ISO-8601 timestamp")
    diagnose.add_argument("--end", default=None, help="exclusive ISO-8601 timestamp")
    diagnose.add_argument("--cohort-dimension", default=None)
    diagnose.add_argument("--format", choices=("json", "markdown"), default="markdown")
    diagnose.add_argument("--output", default="-", help="output path, or '-' for stdout")
    diagnose.add_argument("--timeout", type=float, default=30.0)
    diagnose.set_defaults(func=_diagnose)
    reconcile = subparsers.add_parser(
        "reconcile", help="import a normalized provider bill and render its closure report"
    )
    reconcile.add_argument("--statement", required=True, help="normalized statement JSON path")
    reconcile.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    reconcile.add_argument("--token-env", default="ZEROTH_ECON_TOKEN")
    reconcile.add_argument("--format", choices=("json", "markdown"), default="markdown")
    reconcile.add_argument("--output", default="-", help="output path, or '-' for stdout")
    reconcile.add_argument("--timeout", type=float, default=30.0)
    reconcile.set_defaults(func=_reconcile)
    normalize_openai = subparsers.add_parser(
        "normalize-openai-costs",
        help="convert one complete OpenAI Costs API page into a Zeroth statement",
    )
    normalize_openai.add_argument("--input", required=True, help="OpenAI Costs JSON path")
    normalize_openai.add_argument("--statement-id", required=True)
    normalize_openai.add_argument(
        "--output", default="provider-statement.json", help="output path, or '-' for stdout"
    )
    normalize_openai.set_defaults(func=_normalize_openai_costs)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and run the selected asynchronous command."""
    args = build_parser().parse_args(argv)
    return int(asyncio.run(args.func(args)))


if __name__ == "__main__":
    raise SystemExit(main())
