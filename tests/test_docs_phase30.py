"""Shape tests for Phase 30 docs deliverables.

This file is a scaffold created by Plan 30-01 and populated across Plans
30-02 through 30-05 with real assertions about ``mkdocs.yml``, the
``docs/`` tree, and CI wiring. Keeping the file importable from day one
means downstream plans add assertions rather than creating it from scratch.
"""

from __future__ import annotations

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
    assert "hello.py" in body, "hello.py snippet reference missing"


def test_reference_quadrant_stubs_are_minimal() -> None:
    """Reference stubs must be minimal and flagged for Phase 32 to prevent fake-content rot."""
    for name in ("python-api.md", "http-api.md", "configuration.md"):
        page = DOCS_DIR / "reference" / name
        assert page.exists(), f"{page} missing"
        body = page.read_text(encoding="utf-8")
        assert "TBD" in body or "Phase 32" in body, f"{name} missing TBD/Phase 32 marker"
        assert len(body) < 400, f"{name} is {len(body)} chars; stubs must stay <400 to block rot"
