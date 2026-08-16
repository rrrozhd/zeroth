"""Regenerate console contracts in a temporary directory and report drift."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    frontend = args.frontend.resolve()
    toolchain = Path(__file__).resolve().parents[1]
    compiler = toolchain / "frontend/node_modules/.bin/openapi-typescript"
    pairs = (
        (
            "openapi.json",
            "app/lib/api-types.ts",
            toolchain / "scripts/dump_openapi.py",
        ),
        (
            "openapi.regulus.json",
            "app/lib/api-types.regulus.ts",
            toolchain / "scripts/dump_regulus_openapi.py",
        ),
    )
    drift: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zeroth-api-check-") as raw_tmp:
        tmp = Path(raw_tmp)
        for json_name, ts_name, generator in pairs:
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
                if not tracked.exists() or tracked.read_bytes() != generated.read_bytes():
                    drift.append(str(tracked.relative_to(frontend)))
    if drift:
        sys.stderr.write("DRIFT: " + ", ".join(drift) + "\n")
        return 1
    print("OK: console API contracts are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
