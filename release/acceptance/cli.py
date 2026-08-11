#!/usr/bin/env python3
"""Run the fail-closed acceptance suite against a selected deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import AcceptanceConfig, ResolvedAcceptanceConfig
from .models import AcceptanceContract, AcceptanceReport, ScenarioStatus
from .runner import AcceptanceRunner
from .transport import AcceptanceTransport


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path} is unreadable: {error}") from error


def write_report_atomic(path: Path, report: AcceptanceReport) -> None:
    """Replace the report only after its complete representation is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = report.model_dump_json(indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


async def run_from_paths(
    config_path: Path,
    contract_path: Path,
    *,
    environ: Mapping[str, str],
    transport_factory: Callable[[ResolvedAcceptanceConfig], Any] = AcceptanceTransport,
) -> AcceptanceReport:
    """Validate local inputs, then create the first network-capable object."""
    config = AcceptanceConfig.model_validate(_load_json(config_path)).resolve(environ)
    contract = AcceptanceContract.model_validate(_load_json(contract_path))
    transport = transport_factory(config)
    async with transport:
        return await AcceptanceRunner(config, contract, transport).run()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport_factory: Callable[[ResolvedAcceptanceConfig], Any] = AcceptanceTransport,
) -> int:
    """Run acceptance and return 0 only for a complete passing report."""
    args = _parser().parse_args(argv)
    try:
        report = asyncio.run(
            run_from_paths(
                args.config,
                args.contract,
                environ=os.environ if environ is None else environ,
                transport_factory=transport_factory,
            )
        )
    except (OSError, ValueError, ValidationError) as error:
        print(f"configuration failed: {error}", file=sys.stderr)
        return 2
    write_report_atomic(args.output, report)
    print(f"deployed acceptance: {report.status.value}")
    return 0 if report.status is ScenarioStatus.PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
