from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pydantic

from release.app_certification import candidate_supervisor


def test_root_boundary_under_isolation_loads_only_trusted_dependencies(tmp_path: Path) -> None:
    package = tmp_path / "release" / "app_certification"
    package.mkdir(parents=True)
    (tmp_path / "release" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("from pydantic import BaseModel\n", encoding="utf-8")
    (package / "candidate_supervisor.py").write_text(
        "import json,sys\nprint(json.dumps(sys.path))\n", encoding="utf-8"
    )
    trusted_site = Path(pydantic.__file__).resolve().parents[1]
    candidate_path = tmp_path / "candidate-controlled"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            candidate_supervisor._ROOT_BOUNDARY_BOOTSTRAP,
            str(tmp_path),
            str(trusted_site),
            str(candidate_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    paths = json.loads(completed.stdout.splitlines()[-1])
    assert str(trusted_site) in paths
    assert str(candidate_path) not in paths
