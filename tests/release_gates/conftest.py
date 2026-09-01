from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "release"


def _load_gates() -> None:
    """Register ``release/gates`` as an importable package.

    Deliberately not ``sys.path.insert(0, release/)``: ``release/langgraph``
    would become a PEP 420 namespace portion named ``langgraph``, merged ahead
    of the installed LangGraph library (both are namespace packages), which
    silently changes what ``import langgraph`` resolves to for the whole test
    session. An explicit spec leaves sys.path alone.
    """
    if "gates" in sys.modules:
        return
    package = RELEASE / "gates"
    spec = importlib.util.spec_from_file_location(
        "gates", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gates"] = module
    spec.loader.exec_module(module)


_load_gates()

CLI = RELEASE / "gates" / "cli.py"
MANIFEST_PATH = RELEASE / "gates" / "release-gates.json"

COMMIT = "a" * 40
WHEEL_DIGEST = "sha256:" + "b" * 64
IMAGE_DIGEST = "sha256:" + "c" * 64
CONFIG_DIGEST = "sha256:" + "d" * 64
COMPAT_DIGEST = "sha256:" + "e" * 64


@pytest.fixture
def manifest() -> dict:
    from gates.manifest import load_manifest

    return load_manifest(MANIFEST_PATH)


@pytest.fixture
def candidate() -> dict:
    """A candidate identity carrying every facet any gate can bind."""
    return {
        "schema_version": 1,
        "commit": COMMIT,
        "package": {
            "version": "0.19",
            "artifacts": {"zeroth_core-0.19-py3-none-any.whl": WHEEL_DIGEST},
        },
        "image": {"zeroth-core:v0.19": IMAGE_DIGEST},
        "configuration": CONFIG_DIGEST,
        "compatibility": COMPAT_DIGEST,
    }


#: Evidence bodies that actually look like the kind they claim to be. A fixture
#: that writes "source junit" into a file called a JUnit report would make the
#: suite agree that any file is evidence, which is the hole the validator's
#: shape checks exist to close.
KIND_BODIES = {
    "junit": '<?xml version="1.0"?>\n<testsuites><testsuite name="x" tests="1"/></testsuites>\n',
    "ui": '<?xml version="1.0"?>\n<testsuites><testsuite name="ui" tests="1"/></testsuites>\n',
    "compatibility": '{"release": "0.19", "resolved": {}}\n',
    "benchmark": '{"release": "0.19", "passed": true}\n',
    "sbom": '{"spdxVersion": "SPDX-2.3", "packages": []}\n',
    "provenance": '{"mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3"}\n',
    "security": '{"verified": true}\n',
    "deployment": "readiness ok\ngateway ok\n",
    "economic-acceptance": "{}\n",
    "manual-signoff": "Accepted by an operator.\n",
}


def evidence_body(kind: str) -> str:
    """Return a body that satisfies the validator's shape check for ``kind``."""
    return KIND_BODIES.get(kind, f"{kind} evidence\n")


def write_record(root: Path, gate: dict, candidate: dict, **overrides) -> Path:
    """Write one gate's record, valid unless an override says otherwise."""
    kinds = {}
    for kind in gate["kinds"]:
        suffix = "xml" if kind in ("junit", "ui") else "json" if kind in KIND_BODIES else "txt"
        relative = f"release/evidence/{gate['id']}-{kind}.{suffix}"
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        body = evidence_body(kind)
        if gate["id"] == "remote-acceptance" and kind == "economic-acceptance":
            from gates.identity import identity_digest

            body = json.dumps(
                {
                    "schema_version": 1,
                    "status": "passed",
                    "candidate_digest": identity_digest(candidate),
                    "package": {
                        "name": "zeroth-core",
                        "version": candidate["package"]["version"],
                        "artifact_digest": next(iter(candidate["package"]["artifacts"].values())),
                    },
                    "excluded_distributions": {
                        "zeroth-console": "absent",
                        "zeroth-sdk": "absent",
                    },
                    "diagnostic": {
                        "claim_scope": "observed_economic_exposure",
                        "decision_state": "economic_risk_observed",
                    },
                    "reconciliation": {
                        "reconciliation_state": "reconciled",
                        "unreconciled_billed_usd": "0.00",
                    },
                }
            )
        (root / relative).write_text(body, encoding="utf-8")
        kinds[kind] = relative
    record = {
        "schema_version": 1,
        "gate": gate["id"],
        "status": "passed",
        "identity": {facet: candidate[facet] for facet in gate["binds"]},
        "results": dict.fromkeys(gate["requires"], "passed"),
        "kinds": kinds,
        "generated_at": "2026-08-08T12:00:00+00:00",
    }
    record.update(overrides)
    path = root / gate["record"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def evidence(tmp_path: Path, manifest: dict, candidate: dict) -> Path:
    """A complete evidence tree in which every gate passes."""
    for gate in manifest["gates"]:
        write_record(tmp_path, gate, candidate)
    return tmp_path
