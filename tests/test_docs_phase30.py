"""Shape tests for Phase 30 docs deliverables.

This file is a scaffold created by Plan 30-01 and populated across Plans
30-02 through 30-05 with real assertions about ``mkdocs.yml``, the
``docs/`` tree, and CI wiring. Keeping the file importable from day one
means downstream plans add assertions rather than creating it from scratch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS_DIR = REPO_ROOT / "docs"


def _load_mkdocs_config() -> dict:
    """Load mkdocs.yml as a plain dict.

    mkdocs.yml uses the ``!!python/name`` tag in some setups; for our minimal
    scaffold ``yaml.safe_load`` is sufficient and avoids executing any tags.
    """
    assert MKDOCS_YML.exists(), f"mkdocs.yml missing at {MKDOCS_YML}"
    with MKDOCS_YML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_pyproject() -> dict:
    assert PYPROJECT.exists(), f"pyproject.toml missing at {PYPROJECT}"
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _top_level_nav_labels(nav: list) -> list[str]:
    """Extract the top-level nav entry labels, skipping the Home entry.

    mkdocs nav is a list of either ``{label: file}`` dicts (leaf) or
    ``{label: [children]}`` dicts (section).
    """
    labels: list[str] = []
    for entry in nav:
        if not isinstance(entry, dict):
            continue
        # Each top-level entry has exactly one key
        (label,) = entry.keys()
        if label == "Home":
            continue
        labels.append(label)
    return labels


def test_phase30_scaffold_present() -> None:
    """Placeholder test so pytest can discover the file before plans 02-05 populate it."""
    assert True


# ---------------------------------------------------------------------------
# Plan 30-02: mkdocs config shape
# ---------------------------------------------------------------------------


def test_mkdocs_config_has_four_diataxis_sections() -> None:
    """Nav must expose the canonical four Diataxis section names."""
    config = _load_mkdocs_config()
    nav = config.get("nav")
    assert isinstance(nav, list), "mkdocs.yml must define a nav list"
    labels = set(_top_level_nav_labels(nav))
    expected = {"Tutorials", "How-to Guides", "Concepts", "Reference"}
    assert labels == expected, f"Top-level nav labels {labels} != {expected}"


def test_mkdocs_config_has_search_plugin() -> None:
    """The search plugin must be enabled so SITE-04 (search + sitemap) holds."""
    config = _load_mkdocs_config()
    plugins = config.get("plugins") or []
    # Entries can be either a bare string or a {name: opts} dict.
    names: list[str] = []
    for entry in plugins:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            names.extend(entry.keys())
    assert "search" in names, f"search plugin missing from plugins: {names}"


def test_mkdocs_config_has_snippets_check_paths_true() -> None:
    """pymdownx.snippets must run with check_paths=true and include examples/ in base_path."""
    config = _load_mkdocs_config()
    md_exts = config.get("markdown_extensions") or []
    snippets_cfg: dict | None = None
    for entry in md_exts:
        if isinstance(entry, dict) and "pymdownx.snippets" in entry:
            snippets_cfg = entry["pymdownx.snippets"] or {}
            break
    assert snippets_cfg is not None, "pymdownx.snippets not configured"
    assert snippets_cfg.get("check_paths") is True, (
        f"pymdownx.snippets.check_paths must be true, got {snippets_cfg.get('check_paths')!r}"
    )
    base_path = snippets_cfg.get("base_path") or []
    assert "examples" in base_path, (
        f"pymdownx.snippets.base_path must include 'examples', got {base_path!r}"
    )


def test_mkdocs_site_url_set() -> None:
    """site_url must be set to an https URL so the sitemap is correct."""
    config = _load_mkdocs_config()
    site_url = config.get("site_url") or ""
    assert site_url, "site_url is empty"
    assert site_url.startswith("https://"), f"site_url must be https, got {site_url!r}"


def test_docs_extra_declared_in_pyproject() -> None:
    """pyproject must declare a [docs] optional dependency group including mkdocs-material."""
    data = _load_pyproject()
    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "docs" in extras, "project.optional-dependencies.docs missing"
    joined = " ".join(extras["docs"])
    assert "mkdocs-material" in joined, (
        f"docs extra must include mkdocs-material, got: {extras['docs']}"
    )


def test_landing_page_has_tabbed_split_and_hello_snippet() -> None:
    """DOCS-01: landing page must have Choose-Your-Path tabs and the hello snippet embed."""
    index_path = DOCS_DIR / "index.md"
    assert index_path.exists(), f"{index_path} missing"
    body = index_path.read_text(encoding="utf-8")
    assert '=== "Embed as library"' in body, "Embed as library tab missing"
    assert '=== "Run as service"' in body, "Run as service tab missing"
    assert "--8<--" in body, "pymdownx.snippets scissors token missing"
    assert "00_hello.py" in body, "00_hello.py snippet reference missing"


def test_reference_quadrant_pages_exist() -> None:
    """Reference quadrant pages exist. Phase 32 filled the stubs with real content."""
    for name in ("python-api.md", "http-api.md", "configuration.md"):
        page = DOCS_DIR / "reference" / name
        assert page.exists(), f"{page} missing"


# ---------------------------------------------------------------------------
# Plan 30-03: Getting Started tutorial + examples CI workflow
# ---------------------------------------------------------------------------

EXAMPLES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "examples.yml"
GETTING_STARTED = DOCS_DIR / "tutorials" / "getting-started"


def test_examples_workflow_exists() -> None:
    """The examples workflow must exist and trigger on push/PR to main."""
    assert EXAMPLES_WORKFLOW.exists(), f"{EXAMPLES_WORKFLOW} missing"
    config = yaml.safe_load(EXAMPLES_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the Python bool True; accept both.
    triggers = config.get("on") if "on" in config else config.get(True)
    assert isinstance(triggers, dict), f"workflow triggers missing: {triggers!r}"
    push = triggers.get("push") or {}
    pr = triggers.get("pull_request") or {}
    assert "main" in (push.get("branches") or []), "push trigger must include main"
    assert "main" in (pr.get("branches") or []), "pull_request trigger must include main"


def test_examples_workflow_runs_first_graph_and_approval() -> None:
    """The workflow must invoke the core + approval example scripts by path."""
    body = EXAMPLES_WORKFLOW.read_text(encoding="utf-8")
    assert "examples/01_first_graph.py" in body, "01_first_graph.py not wired into examples.yml"
    assert "examples/20_approval_gate.py" in body, "20_approval_gate.py not wired into examples.yml"


def test_first_graph_page_embeds_example() -> None:
    """02-first-graph.md must embed examples/01_first_graph.py via pymdownx.snippets."""
    page = GETTING_STARTED / "02-first-graph.md"
    assert page.exists(), f"{page} missing"
    body = page.read_text(encoding="utf-8")
    assert "--8<--" in body, "snippets token missing from 02-first-graph.md"
    assert "01_first_graph.py" in body, "01_first_graph.py reference missing from 02-first-graph.md"


def test_approval_page_embeds_example_and_curl() -> None:
    """03-service-and-approval.md must embed 20_approval_gate.py and show the curl path."""
    page = GETTING_STARTED / "03-service-and-approval.md"
    assert page.exists(), f"{page} missing"
    body = page.read_text(encoding="utf-8")
    assert "--8<--" in body, "snippets token missing from 03-service-and-approval.md"
    assert "20_approval_gate.py" in body, "20_approval_gate.py reference missing"
    assert "/approvals/" in body and "/resolve" in body, (
        "curl command for POST /approvals/{id}/resolve not documented"
    )


def test_landing_tabs_link_to_getting_started() -> None:
    """docs/index.md Choose Your Path tabs must link to both tutorial sections."""
    body = (DOCS_DIR / "index.md").read_text(encoding="utf-8")
    assert "tutorials/getting-started/02-first-graph.md" in body, (
        "landing page missing link to 02-first-graph.md"
    )
    assert "tutorials/getting-started/03-service-and-approval.md" in body, (
        "landing page missing link to 03-service-and-approval.md"
    )


# ---------------------------------------------------------------------------
# Plan 30-04: Governance Walkthrough tutorial + example
# ---------------------------------------------------------------------------

GOVERNANCE_PAGE = DOCS_DIR / "tutorials" / "governance-walkthrough.md"
GOVERNANCE_EXAMPLE = REPO_ROOT / "examples" / "26_governance_walkthrough.py"

DOCS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"
README = REPO_ROOT / "README.md"


def test_governance_walkthrough_page_shape() -> None:
    """Governance Walkthrough page must exist and name all three primitives."""
    assert GOVERNANCE_PAGE.exists(), f"{GOVERNANCE_PAGE} missing"
    body = GOVERNANCE_PAGE.read_text(encoding="utf-8")
    assert body.lstrip().startswith("# Governance Walkthrough"), (
        "page must begin with H1 'Governance Walkthrough'"
    )
    lowered = body.lower()
    for keyword in ("approval", "audit", "policy"):
        assert keyword in lowered, f"Governance Walkthrough page missing keyword {keyword!r}"


def test_governance_walkthrough_embeds_example() -> None:
    """Governance Walkthrough page must reference the umbrella walkthrough file."""
    body = GOVERNANCE_PAGE.read_text(encoding="utf-8")
    assert "26_governance_walkthrough.py" in body, (
        "26_governance_walkthrough.py reference missing"
    )


def test_governance_walkthrough_example_covers_three_scenarios() -> None:
    """Umbrella walkthrough + its focused files must cover approval, policy, and audit."""
    assert GOVERNANCE_EXAMPLE.exists(), f"{GOVERNANCE_EXAMPLE} missing"
    # The umbrella now delegates to focused files; merge them for the check.
    focused = [
        REPO_ROOT / "examples" / "20_approval_gate.py",
        REPO_ROOT / "examples" / "21_policy_block.py",
        REPO_ROOT / "examples" / "24_audit_query.py",
        GOVERNANCE_EXAMPLE,
    ]
    merged = "\n".join(f.read_text(encoding="utf-8") for f in focused if f.exists())
    lowered = merged.lower()
    assert "approval" in lowered, "governance surface missing 'approval' reference"
    assert "audit" in lowered, "governance surface missing 'audit' reference"
    assert "Capability" in merged, "governance surface missing Capability enum reference"
    assert "NETWORK_WRITE" in merged, "governance surface missing NETWORK_WRITE denial"


def test_governance_walkthrough_example_runs_without_llm_credentials() -> None:
    """The umbrella walkthrough is now hermetic: it must exit 0 without OPENAI_API_KEY.

    The individual scenarios use ``DeterministicProviderAdapter`` (approval,
    policy) or raw library primitives (audit query), so no LLM call is
    needed for any of them.
    """
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    result = subprocess.run(  # noqa: S603 — trusted local example script
        [sys.executable, str(GOVERNANCE_EXAMPLE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"walkthrough must exit 0, got {result.returncode}; stderr={result.stderr!r}"
    )
    assert "all governance scenarios passed" in result.stdout.lower(), (
        f"expected success banner in stdout, got {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Plan 30-05: docs deploy workflow
# ---------------------------------------------------------------------------


def test_docs_workflow_exists_and_valid_yaml() -> None:
    """docs.yml must exist, parse, declare name 'docs', and trigger on push+PR to main."""
    assert DOCS_WORKFLOW.exists(), f"{DOCS_WORKFLOW} missing"
    config = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    assert config.get("name") == "docs", f"workflow name must be 'docs', got {config.get('name')!r}"
    # PyYAML parses bare `on:` as Python True; accept both.
    triggers = config.get("on") if "on" in config else config.get(True)
    assert isinstance(triggers, dict), f"workflow triggers missing: {triggers!r}"
    push = triggers.get("push") or {}
    pr = triggers.get("pull_request") or {}
    assert "main" in (push.get("branches") or []), "push trigger must include main"
    assert "main" in (pr.get("branches") or []), "pull_request trigger must include main"


def test_docs_workflow_build_is_strict() -> None:
    """docs.yml must run mkdocs build with --strict on every trigger."""
    body = DOCS_WORKFLOW.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in body, "strict mkdocs build step missing"


def test_docs_workflow_deploy_is_main_only() -> None:
    """The deploy job must be gated on push to refs/heads/main only."""
    config = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    deploy_job = config["jobs"].get("deploy")
    assert deploy_job, "no deploy job found"
    condition = deploy_job.get("if") or ""
    assert "github.event_name == 'push'" in condition, (
        f"deploy job must gate on push event, got {condition!r}"
    )
    assert "refs/heads/main" in condition, (
        f"deploy job must gate on refs/heads/main, got {condition!r}"
    )


def test_docs_workflow_has_pages_permission() -> None:
    """Docs workflow needs pages: write and id-token: write for actions/deploy-pages."""
    config = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    perms = config.get("permissions") or {}
    assert isinstance(perms, dict), f"permissions block missing: {perms!r}"
    assert perms.get("pages") == "write", f"pages: write permission missing, got {perms!r}"
    assert perms.get("id-token") == "write", f"id-token: write permission missing, got {perms!r}"


def test_readme_links_to_live_docs() -> None:
    """README.md must link to the live docs URL near the top."""
    assert README.exists(), f"{README} missing"
    body = README.read_text(encoding="utf-8")
    assert "https://rrrozhd.github.io/zeroth-core/" in body, (
        "README.md must link to https://rrrozhd.github.io/zeroth-core/"
    )
