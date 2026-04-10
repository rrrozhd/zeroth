from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def _load_rename_module():
    script_path = ROOT / "scripts" / "rename_to_zeroth_core.py"
    assert script_path.exists(), f"expected rename script at {script_path}"

    spec = importlib.util.spec_from_file_location("rename_to_zeroth_core", script_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rename_script_rewrites_python_imports_and_importlib_calls() -> None:
    module = _load_rename_module()

    source = "\n".join(
        [
            "import importlib",
            "import " + "zeroth",
            "import " + "zeroth" + ".core",
            "import " + "zeroth" + ".graph",
            "from " + "zeroth" + " import storage",
            "from " + "zeroth" + ".core" + " import storage as core_storage",
            "from " + "zeroth" + ".graph" + " import Graph",
            "",
            'runtime_module = importlib.import_module("' + "zeroth" + ".service.entrypoint" + '")',
            "storage_module = importlib.import_module('" + "zeroth" + ".storage')",
            'bootstrap_path = "src/zeroth/core/service/bootstrap.py"',
        ]
    )

    rewritten = module.rewrite_python_source(source)

    assert "import " + "zeroth" + ".core" in rewritten
    assert "import " + "zeroth" + ".core" + ".core" not in rewritten
    assert "import " + "zeroth" + ".core.graph" in rewritten
    assert "from " + "zeroth" + ".core import storage" in rewritten
    assert "from " + "zeroth" + ".core import storage as core_storage" in rewritten
    assert "from " + "zeroth" + ".core.graph import Graph" in rewritten
    assert 'importlib.import_module("' + "zeroth" + '.core.service.entrypoint")' in rewritten
    assert "importlib.import_module('" + "zeroth" + ".core.storage')" in rewritten
    assert 'bootstrap_path = "src/zeroth/core/service/bootstrap.py"' in rewritten


def test_rename_script_rewrites_metadata_and_module_path_strings() -> None:
    module = _load_rename_module()

    pyproject = """
[project]
name = "zeroth"
dependencies = [
    "econ-instrumentation-sdk @ file:///Users/dondoe/coding/regulus/sdk/python",
]

[tool.hatch.build.targets.wheel]
packages = ["src/zeroth"]
""".strip()
    dockerfile = 'CMD ["python", "-m", "zeroth.core.service.entrypoint"]'
    alembic = "[alembic]\nscript_location = src/zeroth/core/migrations"

    rewritten_pyproject = module.rewrite_text_file_contents(Path("pyproject.toml"), pyproject)
    rewritten_dockerfile = module.rewrite_text_file_contents(Path("Dockerfile"), dockerfile)
    rewritten_alembic = module.rewrite_text_file_contents(Path("alembic.ini"), alembic)

    assert 'name = "zeroth-core"' in rewritten_pyproject
    assert 'packages = ["src/zeroth"]' in rewritten_pyproject
    assert (
        "econ-instrumentation-sdk @ file:///Users/dondoe/coding/regulus/sdk/python"
        in rewritten_pyproject
    )
    assert "zeroth.core.service.entrypoint" in rewritten_dockerfile
    assert "src/zeroth/core/migrations" in rewritten_alembic


def test_rename_script_scans_live_scenarios_python_files() -> None:
    module = _load_rename_module()

    with TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        target = root / "live_scenarios" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("from zeroth.core.service.bootstrap import bootstrap_service\n")

        files = module.iter_python_files(root)

    assert target in files
