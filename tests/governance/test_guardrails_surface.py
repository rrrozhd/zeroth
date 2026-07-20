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

import pytest


def test_guardrails_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import guardrails as legacy
    from zeroth.governance import guardrails as canonical

    assert canonical.BlocklistFilter is legacy.BlocklistFilter
    assert canonical.ContentFilter is legacy.ContentFilter
    assert canonical.ContentFinding is legacy.ContentFinding
    assert canonical.ContentGuardrail is legacy.ContentGuardrail
    assert canonical.DeadLetterManager is legacy.DeadLetterManager
    assert canonical.GuardrailConfig is legacy.GuardrailConfig
    assert canonical.GuardrailOutcome is legacy.GuardrailOutcome
    assert canonical.PIIFilter is legacy.PIIFilter
    assert canonical.QuotaEnforcer is legacy.QuotaEnforcer
    assert canonical.TokenBucketRateLimiter is legacy.TokenBucketRateLimiter


def test_guardrail_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.guardrails import config as legacy_config
    from zeroth.core.guardrails import content as legacy_content
    from zeroth.core.guardrails import dead_letter as legacy_dead_letter
    from zeroth.core.guardrails import rate_limit as legacy_rate_limit
    from zeroth.governance.guardrails import config as canonical_config
    from zeroth.governance.guardrails import content as canonical_content
    from zeroth.governance.guardrails import dead_letter as canonical_dead_letter
    from zeroth.governance.guardrails import rate_limit as canonical_rate_limit

    assert canonical_config.GuardrailConfig is legacy_config.GuardrailConfig
    assert canonical_content.ContentGuardrail is legacy_content.ContentGuardrail
    assert canonical_dead_letter.DeadLetterManager is legacy_dead_letter.DeadLetterManager
    assert canonical_rate_limit.QuotaEnforcer is legacy_rate_limit.QuotaEnforcer
    assert canonical_rate_limit.TokenBucketRateLimiter is legacy_rate_limit.TokenBucketRateLimiter


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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.guardrails", "zeroth.core.guardrails"),
        ("zeroth.core.guardrails", "zeroth.governance.guardrails"),
        ("zeroth.governance.guardrails", "zeroth.core.runs"),
        ("zeroth.core.runs", "zeroth.governance.guardrails"),
    ],
)
def test_guardrails_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
