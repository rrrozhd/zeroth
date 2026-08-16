"""Regenerate console contracts in a temporary directory and report drift."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def _resolve_frontend(argument: Path) -> Path:
    root = Path.cwd().resolve()
    frontend = argument.resolve() if argument.is_absolute() else (root / argument).resolve()
    if argument.is_absolute():
        root = frontend.parent
    try:
        frontend.relative_to(root)
    except ValueError as error:
        raise ValueError("frontend path resolves outside the app root") from error
    if not frontend.is_dir():
        raise ValueError("frontend path is not a directory below the app root")
    return frontend


def _generated_pairs(toolchain: Path) -> tuple[tuple[str, str, Path], ...]:
    return (
        ("openapi.json", "app/lib/api-types.ts", toolchain / "scripts/dump_openapi.py"),
        (
            "openapi.regulus.json",
            "app/lib/api-types.regulus.ts",
            toolchain / "scripts/dump_regulus_openapi.py",
        ),
    )


def _find_drift(frontend: Path) -> list[str]:
    toolchain = Path(__file__).resolve().parents[1]
    compiler = toolchain / "frontend/node_modules/.bin/openapi-typescript"
    drift: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zeroth-api-check-") as raw_tmp:
        tmp = Path(raw_tmp)
        for json_name, ts_name, generator in _generated_pairs(toolchain):
            generated_json = tmp / json_name
            generated_ts = tmp / Path(ts_name).name
            _run(
                sys.executable,
                str(generator),
                "--out",
                str(generated_json),
                cwd=toolchain,
            )
            _run(str(compiler), str(generated_json), "-o", str(generated_ts), cwd=frontend)
            for tracked, generated in (
                (frontend / json_name, generated_json),
                (frontend / ts_name, generated_ts),
            ):
                try:
                    tracked.resolve().relative_to(frontend)
                except ValueError as error:
                    raise ValueError(
                        f"frontend artifact {tracked} resolves outside the frontend root"
                    ) from error
                if not tracked.exists() or tracked.read_bytes() != generated.read_bytes():
                    drift.append(str(tracked.relative_to(frontend)))
    return drift


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    drift = _find_drift(_resolve_frontend(args.frontend))
    if drift:
        sys.stderr.write("DRIFT: " + ", ".join(drift) + "\n")
        return 1
    print("OK: console API contracts are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
