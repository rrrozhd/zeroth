"""Service-layer client for producing a headless economic diagnostic artifact."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from zeroth.integrations.http.factory import aclose_all, governed_async_client

_STATEMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_MAX_USD_AMOUNT = Decimal("10000000000")


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
        f"| Measured failed-run exposure | {_money(report['measured_failure_exposure_usd'])} |",
        f"| Estimated failed-run exposure | {_money(report['estimated_failure_exposure_usd'])} |",
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
        (
            "**Reconciliation state:** "
            f"{str(report['reconciliation_state']).replace('_', ' ')}"
        ),
        f"**Statement digest:** `{report['statement_digest']}`",
        f"**Allocation method:** {str(report['allocation_method']).replace('_', ' ')}",
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


def _normalize_openai_costs_page(
    page: dict[str, Any], *, statement_id: str
) -> dict[str, Any]:
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
                not isinstance(project_id, str)
                or not project_id
                or len(project_id) > 256
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
            'demo failed: install the economic service dependencies with '
            '`pip install "zeroth-core[regulus]"` '
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


def build_parser() -> argparse.ArgumentParser:
    """Build the headless economic-debugger command-line interface."""
    parser = argparse.ArgumentParser(
        prog="zeroth-econ",
        description="Debug workflow economics and reconcile provider bills without the UI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
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
