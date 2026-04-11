"""Generate the Configuration Reference markdown from pydantic-settings.

Introspects :class:`zeroth.core.config.settings.ZerothSettings` and every
nested ``BaseModel`` sub-section, emitting one markdown table per section
with columns ``Env Var | Type | Default | Secret | Description``.

Usage::

    uv run python scripts/dump_config.py                          # writes docs/reference/configuration.md
    uv run python scripts/dump_config.py --out custom/path.md
    uv run python scripts/dump_config.py --check                  # drift gate, exits 1 if stale

The script is CI-gated via ``--check``. The committed
``docs/reference/configuration.md`` MUST be regenerated with this script;
hand edits will be overwritten.
"""

from __future__ import annotations

import argparse
import sys
import types
import typing
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel, SecretStr
from pydantic_core import PydanticUndefined


PREAMBLE = """# Configuration Reference

Every Zeroth setting is loaded from (in priority order): environment variables (`ZEROTH_` prefix, nested via `__`), a local `.env` file, then `zeroth.yaml`. This reference is auto-generated from `zeroth.core.config.settings` via `scripts/dump_config.py` — **do not edit by hand**.

CI runs `python scripts/dump_config.py --check` on every PR and fails if this file is stale.
"""


def _annotation_contains_secret(annotation: Any) -> bool:
    """Return True if ``annotation`` is, or contains, :class:`SecretStr`."""
    if annotation is SecretStr:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(_annotation_contains_secret(arg) for arg in get_args(annotation))


def _render_annotation(annotation: Any) -> str:
    """Render a pydantic field annotation as a short human-readable string."""
    if annotation is type(None):
        return "None"
    if annotation is SecretStr:
        return "SecretStr"
    if isinstance(annotation, type):
        return annotation.__name__
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is None:
        # Fallback: strip typing/module prefixes.
        text = str(annotation)
        for prefix in ("typing.", "pydantic.types."):
            text = text.replace(prefix, "")
        return text
    # Union / Optional — render with ``|``.
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_render_annotation(arg) for arg in args)
    # Generic container (list[str], dict[str, int], etc.).
    origin_name = getattr(origin, "__name__", None) or str(origin).replace("typing.", "")
    rendered_args = ", ".join(_render_annotation(arg) for arg in args)
    return f"{origin_name}[{rendered_args}]" if rendered_args else origin_name


def _render_default(field: Any) -> str:
    """Render a field default value for the markdown table."""
    default = field.default
    if default is PydanticUndefined:
        factory = field.default_factory
        if factory is None:
            return "—"
        try:
            default = factory()
        except Exception:  # pragma: no cover - defensive
            return "—"
    if _annotation_contains_secret(field.annotation) and default is not None:
        return "`***`"
    if default is None:
        return "`None`"
    if isinstance(default, str):
        return f'`"{default}"`' if default else '`""`'
    if isinstance(default, bool):
        return f"`{default}`"
    if isinstance(default, (int, float)):
        return f"`{default}`"
    if isinstance(default, (list, dict, tuple, set)):
        return f"`{default!r}`"
    return f"`{default!r}`"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_section(section_name: str, model_cls: type[BaseModel]) -> str:
    """Render a single markdown section for ``model_cls``."""
    heading_title = section_name.replace("_", " ").title()
    lines: list[str] = [f"## {heading_title}", ""]

    doc = (model_cls.__doc__ or "").strip()
    if doc:
        # Use only the first non-empty line of the class docstring as the prose blurb.
        first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")
        if first_line:
            lines.append(first_line)
            lines.append("")

    lines.append("| Env Var | Type | Default | Secret | Description |")
    lines.append("| --- | --- | --- | --- | --- |")

    for field_name, field in model_cls.model_fields.items():
        env_var = f"ZEROTH_{section_name.upper()}__{field_name.upper()}"
        type_str = _render_annotation(field.annotation).replace("|", "\\|")
        default_str = _render_default(field)
        secret_mark = "✓" if _annotation_contains_secret(field.annotation) else ""
        description = _escape_cell(field.description or "")
        lines.append(
            f"| `{env_var}` | `{type_str}` | {default_str} | {secret_mark} | {description} |"
        )

    lines.append("")
    return "\n".join(lines)


def render_markdown() -> str:
    """Render the full configuration reference markdown document."""
    # Deferred import so ``--help`` stays fast and side-effect free.
    from zeroth.core.config.settings import ZerothSettings

    parts: list[str] = [PREAMBLE]

    for field_name, field in ZerothSettings.model_fields.items():
        annotation = field.annotation
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            continue
        parts.append(_render_section(field_name, annotation))

    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/reference/configuration.md from pydantic-settings.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/reference/configuration.md"),
        help="Output file (default: docs/reference/configuration.md)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the generated output would differ from --out on disk.",
    )
    args = parser.parse_args()

    generated = render_markdown()

    if args.check:
        if not args.out.exists():
            sys.stderr.write(f"DRIFT: {args.out} does not exist\n")
            return 1
        current = args.out.read_text()
        if current != generated:
            sys.stderr.write(
                f"DRIFT: {args.out} is stale — rerun `python scripts/dump_config.py`\n"
            )
            return 1
        sys.stdout.write(f"OK: {args.out} is up to date\n")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(generated)
    sys.stdout.write(f"Wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
