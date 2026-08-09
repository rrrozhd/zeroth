from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "scripts" / "dump_openapi.py"
REGULUS = ROOT / "scripts" / "dump_regulus_openapi.py"
HOOKS = ROOT / "scripts" / "mkdocs_hooks.py"
ASSET_RELPATH = Path("assets") / "openapi" / "zeroth-core-openapi.json"
SPEC_PAGE = "reference/http-api.md"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _load_hooks() -> ModuleType:
    """Import the mkdocs hook module by path, the way mkdocs itself loads it."""
    spec = importlib.util.spec_from_file_location("zeroth_mkdocs_hooks", HOOKS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _viewer_page(config: str, prose: str = "") -> str:
    """A page fragment shaped like the real one: prose plus the viewer's inline script."""
    return f"<p>{prose}</p><script>window.ui = SwaggerUIBundle({{{config}}});</script>"


def _spec_page():
    return SimpleNamespace(file=SimpleNamespace(url="reference/http-api/", src_uri=SPEC_PAGE))


def test_main_generator_uses_canonical_service_import() -> None:
    source = MAIN.read_text()
    assert "from zeroth.service.app import create_app" in source
    assert "from zeroth.core.service.app import create_app" not in source


def test_main_generator_is_deterministic_and_detects_drift(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run(MAIN, "--out", str(first)).returncode == 0
    assert _run(MAIN, "--out", str(second)).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert _run(MAIN, "--out", str(first), "--check").returncode == 0
    first.write_text("{}\n")
    drift = _run(MAIN, "--out", str(first), "--check")
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr


def test_parent_schema_exposes_proxy_but_not_mounted_regulus_routes(tmp_path: Path) -> None:
    output = tmp_path / "main.json"
    assert _run(MAIN, "--out", str(output)).returncode == 0
    paths = json.loads(output.read_text())["paths"]
    assert "/v1/econ/regulus/dashboard/kpis" in paths
    assert not any(path.startswith("/regulus/") for path in paths)


def test_docs_hook_generates_the_asset_the_http_api_page_links_to(tmp_path: Path) -> None:
    """ZER-20: a strict docs build must not depend on a prior CI generate step.

    ``docs/reference/http-api.md`` links to the OpenAPI asset, which is gitignored
    rather than committed. With no hook the target is absent in any clean checkout
    and ``mkdocs build --strict`` promotes the dangling link to a build error.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _load_hooks().on_pre_build({"docs_dir": str(docs_dir)})

    asset = docs_dir / ASSET_RELPATH
    assert asset.is_file(), f"hook did not generate {ASSET_RELPATH}"

    reference = tmp_path / "reference.json"
    assert _run(MAIN, "--out", str(reference)).returncode == 0
    assert asset.read_bytes() == reference.read_bytes(), (
        "hook output drifted from the CLI generator — there must be one source of truth"
    )


def test_docs_hook_leaves_an_already_current_asset_untouched(tmp_path: Path) -> None:
    """The hook writes into ``docs/``, which ``mkdocs serve`` watches.

    Rewriting an identical file on every build would retrigger the watcher and
    rebuild forever, so an unchanged spec must not be written at all.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    hooks = _load_hooks()
    config = {"docs_dir": str(docs_dir)}
    hooks.on_pre_build(config)

    asset = docs_dir / ASSET_RELPATH
    os.utime(asset, (0, 0))
    hooks.on_pre_build(config)
    assert asset.stat().st_mtime == 0, "hook rewrote an unchanged asset"


@pytest.mark.parametrize(
    ("page_url", "expected"),
    [
        ("reference/http-api/", "../../assets/openapi/zeroth-core-openapi.json"),
        ("reference/http-api.html", "../assets/openapi/zeroth-core-openapi.json"),
    ],
)
def test_docs_hook_substitutes_the_spec_url_for_both_url_modes(page_url, expected) -> None:
    """ZER-20: the build writes the viewer's URL, so it is right in both URL modes.

    With ``use_directory_urls`` on the page is served one level deeper than its source
    path, so the two modes need different relative URLs. A hand-written URL is wrong in
    whichever mode it was not written for -- which is how the published viewer ended up
    loading nothing.
    """
    hooks = _load_hooks()
    page = SimpleNamespace(file=SimpleNamespace(url=page_url, src_uri=SPEC_PAGE))
    output = hooks.on_post_page(_viewer_page('url: "@@ZEROTH_OPENAPI_SPEC_URL@@",'), page=page)
    assert hooks.active_spec_urls(output) == [expected]


def test_docs_hook_fails_the_build_when_the_spec_page_loses_its_token() -> None:
    """A hand-edited URL must abort the build, not ship a viewer that silently 404s."""
    hooks = _load_hooks()
    with pytest.raises(RuntimeError, match="ZEROTH_OPENAPI_SPEC_URL"):
        hooks.on_post_page(
            _viewer_page('url: "../assets/openapi/zeroth-core-openapi.json",'),
            page=_spec_page(),
        )


@pytest.mark.parametrize(
    "legitimate",
    [
        'url: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        "url: `@@ZEROTH_OPENAPI_SPEC_URL@@`,",
        'url : "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        '"url": "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        "'url': '@@ZEROTH_OPENAPI_SPEC_URL@@',",
        '// see https://example.com/url: notes\nurl: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        '/* url: "old" */\nurl: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        'deepLinking: true, url: "@@ZEROTH_OPENAPI_SPEC_URL@@", layout: "BaseLayout",',
    ],
)
def test_docs_hook_accepts_the_spellings_a_contributor_may_write(legitimate) -> None:
    """A guard that rejects valid edits gets deleted by the next contributor."""
    hooks = _load_hooks()
    output = hooks.on_post_page(_viewer_page(legitimate), page=_spec_page())
    assert "../../assets/openapi/zeroth-core-openapi.json" in output


def test_docs_hook_is_not_confused_by_apostrophes_in_prose() -> None:
    """Scanning is scoped to the script, so an apostrophe cannot open a JS string.

    A whole-page quote scanner reads "doesn't" as an opening quote and mis-parses
    everything after it, which can make a valid binding vanish or a commented one count.
    """
    hooks = _load_hooks()
    prose = "The spec doesn't live in git; it's generated. See the note above."
    output = hooks.on_post_page(
        _viewer_page('url: "@@ZEROTH_OPENAPI_SPEC_URL@@",', prose=prose),
        page=_spec_page(),
    )
    assert hooks.active_spec_urls(output) == ["../../assets/openapi/zeroth-core-openapi.json"]


@pytest.mark.parametrize(
    "broken",
    [
        '// url: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        '/* url: "@@ZEROTH_OPENAPI_SPEC_URL@@", */',
        'urlx: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        'spec_url: "@@ZEROTH_OPENAPI_SPEC_URL@@",',
        '"spec_url": "@@ZEROTH_OPENAPI_SPEC_URL@@",',
    ],
)
def test_docs_hook_rejects_a_token_that_is_present_but_not_bound(broken) -> None:
    """Substituting the token is not the same as wiring the viewer.

    A token in a commented-out line, or on a differently-named property such as
    ``spec_url``, substitutes cleanly and still leaves Swagger UI with no URL.
    """
    hooks = _load_hooks()
    with pytest.raises(RuntimeError, match="no active `url` property"):
        hooks.on_post_page(_viewer_page(broken), page=_spec_page())


@pytest.mark.parametrize(
    "formatting",
    [
        "<script>window.ui = SwaggerUIBundle ({URL});</script>",
        "<script>window.ui = SwaggerUIBundle(\n  {URL}\n);</script>",
        "<script>window.ui = SwaggerUIBundle // set up the viewer\n({URL});</script>",
        "<script>var t = `see https://example.com/docs`; "
        "window.ui = SwaggerUIBundle({URL});</script>",
    ],
)
def test_docs_hook_tolerates_reformatting_around_the_viewer_call(formatting) -> None:
    """Recognition must not hinge on exact text, or valid reformatting breaks the build.

    A space or newline before the paren is ordinary formatting, and a template literal
    holding a URL must not be read as the start of a comment that swallows the binding
    after it.
    """
    hooks = _load_hooks()
    page = formatting.replace("{URL}", 'url: "@@ZEROTH_OPENAPI_SPEC_URL@@",')
    output = hooks.on_post_page(page, page=_spec_page())
    assert hooks.active_spec_urls(output) == ["../../assets/openapi/zeroth-core-openapi.json"]


def test_docs_hook_requires_the_viewer_call_itself() -> None:
    """A page whose viewer call is gone has no configuration to be correct."""
    hooks = _load_hooks()
    commented_out = (
        '<script>/* window.ui = SwaggerUIBundle({url: "@@ZEROTH_OPENAPI_SPEC_URL@@"}); */</script>'
    )
    with pytest.raises(RuntimeError, match="no active `url` property"):
        hooks.on_post_page(commented_out, page=_spec_page())


def test_docs_hook_comment_stripping_keeps_url_literals_intact() -> None:
    """`//` appears inside every https:// literal, so quotes must be tracked."""
    hooks = _load_hooks()
    page = _viewer_page('url: "https://example.com/a.json",')
    assert hooks.active_spec_urls(page) == ["https://example.com/a.json"]


def test_docs_hook_leaves_other_pages_alone() -> None:
    """Only the viewer page is required to carry the token."""
    hooks = _load_hooks()
    page = SimpleNamespace(file=SimpleNamespace(url="concepts/graph/", src_uri="concepts/graph.md"))
    assert hooks.on_post_page("<p>no token here</p>", page=page) == "<p>no token here</p>"


def test_docs_hook_reuses_the_cli_generator_and_is_not_itself_published() -> None:
    """One stub bootstrap, one generator, and nothing extra copied into the site."""
    source = HOOKS.read_text()
    assert "generate_spec_text" in source
    assert "SimpleNamespace" not in source, "hook must not re-implement the stub bootstrap"
    assert "create_app" not in source, "hook must not build its own app"
    assert not (ROOT / "docs" / "mkdocs_hooks.py").exists(), (
        "a hook inside docs/ would be collected and published as documentation"
    )


def test_regulus_generator_is_deterministic_and_detects_drift(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    generated = _run(REGULUS, "--out", str(first))
    assert generated.returncode == 0, generated.stderr
    assert _run(REGULUS, "--out", str(second)).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert "/v1/dashboard/kpis" in json.loads(first.read_text())["paths"]
    assert _run(REGULUS, "--out", str(first), "--check").returncode == 0
    first.write_text("{}\n")
    drift = _run(REGULUS, "--out", str(first), "--check")
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr
