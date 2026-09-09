import pytest

from scripts.check_docs_references import _install_violations, scan_markdown
from tests.test_docs_references import REPO_ROOT


@pytest.mark.parametrize(
    "version", ["0.1.0a1", "0.1.0b2", "0.1.0rc3", "0.1.0.dev4", "0.1.0.post1", "0.1.0+local.1"]
)
def test_exact_declared_release_suffix_is_preserved(version):
    projects = ({"name": "zeroth-sdk", "version": version},)
    assert (
        _install_violations(f'pip install "zeroth-sdk=={version}"', "docs/sdk.md", 1, projects)
        == []
    )


@pytest.mark.parametrize("supplied", ["0.1.0", "0.1.0a2", "0.1.0a1garbage"])
def test_other_release_is_not_accepted_as_declared_prerelease(supplied):
    violations = _install_violations(
        f'pip install "zeroth-sdk=={supplied}"',
        "docs/sdk.md",
        1,
        ({"name": "zeroth-sdk", "version": "0.1.0a1"},),
    )
    assert len(violations) == 1
    assert violations[0].kind == "install-target"


def test_sdk_example_environment_names_are_known_but_typos_are_not():
    markdown = """Set `ZEROTH_API_KEY` and `ZEROTH_BASE_URL` for this client example.
```python
client = ZerothClient(api_key=os.environ["ZEROTH_API_KEY"],
    base_url=os.environ["ZEROTH_BASE_URL"])
```
Set `ZEROTH_API_KEEY` only if it exists.
"""
    violations = scan_markdown(markdown, "docs/sdk-example.md", REPO_ROOT)
    assert [(item.kind, item.target) for item in violations] == [("environment", "ZEROTH_API_KEEY")]
