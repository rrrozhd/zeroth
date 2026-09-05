"""The external lifecycle probe must not race unrelated container removal."""

import ast
from pathlib import Path
import subprocess


def test_probe_ownership_uses_one_snapshot_and_excludes_other_image_aliases():
    # Load only this pure helper; importing the executable probe starts a server.
    path = Path(__file__).with_name("_real_http_lifecycle_probe.py")
    tree = ast.parse(path.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "owned")
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "inspect":
            raise subprocess.CalledProcessError(1, ["docker", *args], stderr="no such object")
        if "--format" in args:
            return "owned-id owned:tag\nunrelated-id another:tag"
        return "owned-id\nunrelated-id"

    scope = {"docker": docker, "tag": "owned:tag"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope)
    assert scope["owned"]() == ["owned-id"]
    assert len(calls) == 1
    assert "--format" in calls[0]
