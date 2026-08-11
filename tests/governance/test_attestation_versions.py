"""Tests for the adapter-version constant and mixed-version agreement rule.

ZER-8 S2 requires mixed-version behaviour to be *defined and tested*, not just
detected. ``classify_version_agreement`` fails closed: any missing side reads
as ``UNKNOWN`` rather than as a match, and ``permits_full_enforcement`` must
read as False for everything except the exact-match case -- including
``UNKNOWN`` -- so an absent version can never be mistaken for an agreement.
"""

from __future__ import annotations

import pytest

from zeroth.governance.attestations.versions import (
    ADAPTER_VERSION,
    VersionAgreement,
    classify_version_agreement,
    permits_full_enforcement,
)


def _classify(
    *,
    expected_graph: str | None = "g1",
    actual_graph: str | None = "g1",
    expected_adapter: str | None = "1.0",
    actual_adapter: str | None = "1.0",
) -> VersionAgreement:
    return classify_version_agreement(
        expected_graph_version=expected_graph,
        actual_graph_version=actual_graph,
        expected_adapter_version=expected_adapter,
        actual_adapter_version=actual_adapter,
    )


class TestClassifyVersionAgreement:
    def test_exact_match_is_match(self) -> None:
        result = _classify()
        assert result is VersionAgreement.MATCH

    def test_adapter_only_mismatch(self) -> None:
        result = _classify(actual_adapter="2.0")
        assert result is VersionAgreement.ADAPTER_MISMATCH

    def test_graph_only_mismatch(self) -> None:
        result = _classify(actual_graph="g2")
        assert result is VersionAgreement.GRAPH_MISMATCH

    def test_both_mismatched(self) -> None:
        result = _classify(actual_graph="g2", actual_adapter="2.0")
        assert result is VersionAgreement.BOTH_MISMATCH

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"expected_graph": None},
            {"expected_graph": ""},
            {"actual_graph": None},
            {"actual_graph": ""},
            {"expected_adapter": None},
            {"expected_adapter": ""},
            {"actual_adapter": None},
            {"actual_adapter": ""},
        ],
    )
    def test_missing_or_empty_side_is_unknown(self, kwargs: dict[str, str | None]) -> None:
        result = _classify(**kwargs)
        assert result is VersionAgreement.UNKNOWN

    def test_all_missing_is_unknown(self) -> None:
        result = classify_version_agreement(
            expected_graph_version=None,
            actual_graph_version=None,
            expected_adapter_version=None,
            actual_adapter_version=None,
        )
        assert result is VersionAgreement.UNKNOWN

    def test_comparison_is_exact_string_equality_not_semver(self) -> None:
        # "1.0" vs "1.0.0" must NOT be treated as equivalent: no range logic.
        result = _classify(actual_adapter="1.0.0")
        assert result is VersionAgreement.ADAPTER_MISMATCH


class TestPermitsFullEnforcement:
    def test_match_permits_enforcement(self) -> None:
        assert permits_full_enforcement(VersionAgreement.MATCH) is True

    @pytest.mark.parametrize(
        "member",
        [m for m in VersionAgreement if m is not VersionAgreement.MATCH],
    )
    def test_every_non_match_member_forbids_enforcement(self, member: VersionAgreement) -> None:
        assert permits_full_enforcement(member) is False


class TestAdapterVersionConstant:
    def test_adapter_version_is_nonempty_string(self) -> None:
        assert isinstance(ADAPTER_VERSION, str)
        assert ADAPTER_VERSION != ""

    def test_inventory_encoding_change_advanced_the_adapter_contract(self) -> None:
        assert ADAPTER_VERSION == "2.0"
