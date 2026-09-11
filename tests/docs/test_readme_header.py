"""README branding and workflow status remain visible, not just linked."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_readme_header_uses_svg_lockup() -> None:
    header = (ROOT / "README.md").read_text().split("## What it does", 1)[0]
    assert '<source media="(prefers-color-scheme: dark)"' in header
    filename = "zeroth-logo-dark.svg"
    for attribute in ("srcset", "src"):
        assert f'{attribute}="docs/assets/logo/{filename}"' in header
    assert (ROOT / "docs/assets/logo" / filename).is_file()
    assert 'src="docs/assets/logo/zeroth-mark-v2.png"' not in header


def test_readme_renders_live_main_branch_docs_and_ci_badges() -> None:
    header = (ROOT / "README.md").read_text().split("## What it does", 1)[0]
    for workflow, label in (("docs", "Docs (main)"), ("ci", "CI (main)")):
        assert (ROOT / f".github/workflows/{workflow}.yml").is_file()
        url = f"https://github.com/rrrozhd/zeroth/actions/workflows/{workflow}.yml"
        assert f'href="{url}?query=branch%3Amain"' in header
        assert f'src="{url}/badge.svg?branch=main"' in header
        assert f'alt="{label}"' in header
