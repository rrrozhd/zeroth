"""The ZER-32 gate documents its scope, operators, and invalidation boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUIDE = (ROOT / "docs/how-to/deployment/release-gates.md").read_text(encoding="utf-8")
POLICY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def test_security_gate_guide_assigns_every_trigger_responsibility() -> None:
    for heading in ("### Pull request", "### Nightly", "### Release candidate", "### Manual"):
        assert heading in GUIDE
    assert "pr-critical" in GUIDE
    assert "release-candidate" in GUIDE
    assert "Redis" in GUIDE
    assert "PostgreSQL" in GUIDE
    assert "Docker" in GUIDE
    assert "skipped" in GUIDE


def test_security_docs_make_repository_ingress_absence_an_explicit_invalidatable_proof() -> None:
    combined = f"{GUIDE}\n{POLICY}".lower()

    assert "repository installation" in combined
    assert "checkout" in combined
    assert "absent" in combined
    assert "invalidates" in combined
    assert "trusted materializer" in combined
