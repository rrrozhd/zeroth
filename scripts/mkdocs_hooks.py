"""MkDocs build hooks for the Zeroth documentation site.

Registered from ``mkdocs.yml`` via ``hooks:``. This file deliberately lives
outside ``docs/`` -- anything under the docs directory is collected as a
documentation file and copied into ``site/``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = Path(__file__).resolve().parent
_GENERATOR = _SCRIPTS_DIR / "dump_openapi.py"
_ASSET_RELPATH = Path("assets") / "openapi" / "zeroth-core-openapi.json"

#: Site-root-relative URL of the generated asset, as MkDocs sees it.
ASSET_URL = _ASSET_RELPATH.as_posix()
#: Page that embeds the Swagger UI viewer.
SPEC_PAGE_SRC_URI = "reference/http-api.md"
#: Token the viewer's ``url`` field carries in source; replaced at build time.
SPEC_URL_TOKEN = "@@ZEROTH_OPENAPI_SPEC_URL@@"

#: A ``url`` property binding. Tolerates the spellings a contributor may legitimately
#: write -- ``url:``, ``url :``, ``"url":``, ``'url':`` -- and either quote style. The
#: lookbehind keeps ``spec_url:`` from matching on its ``url`` suffix: that is a
#: different property, and Swagger UI would have no URL at all. The value may be
#: delimited by any of JavaScript's three string quotes, backticks included, and the
#: closing delimiter must match the opening one.
_URL_BINDING = re.compile(r"""(?<![\w$])["']?url["']?\s*:\s*(?P<q>["'`])(?P<value>[^"'`]+)(?P=q)""")

#: Inline script bodies. Scanning is confined to these so prose elsewhere on the page --
#: an apostrophe in "doesn't", say -- is never mistaken for a JavaScript quote.
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)

#: The call whose configuration owns the spec URL. Whitespace before the paren is
#: legitimate formatting, so recognition must not hinge on the exact text.
_VIEWER_CALL = re.compile(r"SwaggerUIBundle\s*\(")


def _without_js_comments(source: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, leaving string literals intact.

    Needed because a commented-out binding is not a binding: substituting the token
    inside ``// url: "@@…@@"`` would otherwise look like a correctly wired viewer while
    Swagger UI actually receives no URL at all. Naive comment stripping cannot be used
    here -- ``//`` occurs inside every ``https://`` literal -- so quotes are tracked,
    backticks included: a template literal holding a URL would otherwise be read as the
    start of a comment and swallow the real binding after it.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(source):
        char = source[i]
        if quote is not None:
            out.append(char)
            if char == "\\" and i + 1 < len(source):
                out.append(source[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end == -1 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = len(source) if end == -1 else end + 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def active_spec_urls(source: str) -> list[str]:
    """Return every ``url`` value the Swagger UI viewer is actually configured with.

    Scoped to the viewer's own call inside an inline ``<script>``: a value that is
    commented out, attached to a differently-named property, or sitting in prose is not
    a configuration and must not count as one. A page whose viewer call has been removed
    or commented out yields an empty list, which is what makes the build fail closed.
    """
    bound: list[str] = []
    for block in _SCRIPT_BLOCK.findall(source):
        code = _without_js_comments(block)
        call = _VIEWER_CALL.search(code)
        if call is not None:
            bound.extend(m.group("value") for m in _URL_BINDING.finditer(code[call.end() :]))
    return bound


def _load_generator() -> ModuleType:
    """Import ``dump_openapi.py`` by path.

    ``scripts/`` is not a package, so a plain import would need sys.path
    surgery. Loading by path keeps the dependency explicit and local.
    """
    spec = importlib.util.spec_from_file_location("zeroth_dump_openapi", _GENERATOR)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load the OpenAPI generator from {_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def on_pre_build(config, **_kwargs) -> None:
    """Generate the OpenAPI asset that the HTTP API reference links to.

    ``docs/reference/http-api.md`` links to
    ``assets/openapi/zeroth-core-openapi.json``, which is generated from the
    live FastAPI app rather than committed (see ``.gitignore``). Before this
    hook existed the target was absent in any build not preceded by the CI
    generate step, so ``mkdocs build --strict`` promoted the dangling link to
    an error and the strict build was unusable for catching real documentation
    rot (ZER-20).

    ``on_pre_build`` runs before mkdocs collects the docs tree, so a file
    written here is picked up as an ordinary asset.

    The spec is written only when it differs from what is already on disk.
    ``mkdocs serve`` watches the docs directory, so an unconditional write
    would retrigger the watcher and rebuild in a loop.
    """
    target = Path(config["docs_dir"]) / _ASSET_RELPATH
    text = _load_generator().generate_spec_text()

    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def on_post_page(output: str, *, page, config=None, **_kwargs) -> str:
    """Write the Swagger UI's spec URL into the rendered page.

    MkDocs rewrites relative URLs in Markdown but not inside a ``<script>``, and the
    page's depth depends on ``use_directory_urls`` -- ``reference/http-api/`` when it is
    on, ``reference/http-api.html`` when it is off. So any URL written by hand in the
    viewer's config is wrong in at least one mode, which is exactly how ZER-20's
    published Swagger UI ended up loading nothing.

    Rather than hand-writing a URL and testing that it is right, the build computes it
    with MkDocs' own ``get_relative_url`` and substitutes it here. The rendered page
    therefore carries a plain resolved string with nothing left to get wrong, and a
    missing token fails the build instead of shipping a viewer that silently 404s.

    This only rewrites the returned HTML; it never writes into ``docs/``, so
    ``on_pre_build`` stays the only writer and ``mkdocs serve`` cannot be driven into a
    rebuild loop.
    """
    from mkdocs.utils import get_relative_url

    is_spec_page = page.file.src_uri == SPEC_PAGE_SRC_URI
    if SPEC_URL_TOKEN not in output:
        if is_spec_page:
            raise RuntimeError(
                f"{SPEC_PAGE_SRC_URI} no longer contains {SPEC_URL_TOKEN}. The Swagger "
                "UI's spec URL must stay a build-substituted token -- a hand-written "
                "relative URL is wrong under one of the two use_directory_urls modes "
                "(ZER-20)."
            )
        return output

    spec_url = get_relative_url(ASSET_URL, page.file.url)
    output = output.replace(SPEC_URL_TOKEN, spec_url)

    # Substituting the token is not the same as wiring the viewer: a token sitting in a
    # commented-out line substitutes cleanly and leaves Swagger UI with no URL. Confirm
    # the value is actually bound before letting the build succeed.
    if is_spec_page and spec_url not in active_spec_urls(output):
        raise RuntimeError(
            f"{SPEC_PAGE_SRC_URI} substituted {spec_url} but no active `url` property "
            "carries it -- the Swagger UI binding is commented out or malformed, so the "
            "published viewer would load nothing (ZER-20)."
        )
    return output
