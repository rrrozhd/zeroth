"""Canonical import surface for the governance guardrails package.

Non-golden boundary tests for the Task 13 guardrails move: the canonical
``zeroth.governance.guardrails`` package must publish the same objects the
legacy ``zeroth.core.guardrails`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.

One seam is pinned alongside the move: ``DeadLetterManager`` speaks the
runs-table dead-letter vocabulary through a locally pinned literal instead
of importing the persistence module (the LeaseManager precedent), so the
literal must stay equal to the repository's own constant.
"""

from __future__ import annotations

import subprocess
import sys


def test_guardrails_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.governance.guardrails as canonical

    expected = {
        "BlocklistFilter",
        "ContentFilter",
        "ContentFinding",
        "ContentGuardrail",
        "DeadLetterManager",
        "GuardrailConfig",
        "GuardrailOutcome",
        "PIIFilter",
        "QuotaEnforcer",
        "TokenBucketRateLimiter",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.governance.guardrails no longer publishes: {missing}"


def test_guardrail_submodules_publish_their_names() -> None:
    from zeroth.governance.guardrails import config as canonical_config
    from zeroth.governance.guardrails import content as canonical_content
    from zeroth.governance.guardrails import dead_letter as canonical_dead_letter
    from zeroth.governance.guardrails import rate_limit as canonical_rate_limit

    assert hasattr(canonical_config, "GuardrailConfig")
    assert hasattr(canonical_content, "ContentGuardrail")
    assert hasattr(canonical_dead_letter, "DeadLetterManager")
    assert hasattr(canonical_rate_limit, "QuotaEnforcer")
    assert hasattr(canonical_rate_limit, "TokenBucketRateLimiter")


def test_dead_letter_reason_literal_matches_the_repository_vocabulary() -> None:
    from zeroth.governance.guardrails import dead_letter
    from zeroth.integrations.persistence.runs import run_repository

    assert dead_letter.DEAD_LETTER_REASON == run_repository.DEAD_LETTER_REASON


def test_guardrails_package_stays_off_the_persistence_import_path() -> None:
    probe = (
        "import sys\n"
        "import zeroth.governance.guardrails\n"
        "assert 'zeroth.integrations.persistence.runs.run_repository' not in sys.modules, (\n"
        "    'guardrails pulled the concrete run repository'\n"
        ")\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_guardrails_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.governance.guardrails"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
