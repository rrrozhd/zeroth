"""Tests for context window models and error hierarchy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.core.context_window.models import (
    CompactionResult,
    CompactionState,
    ContextWindowSettings,
)
from zeroth.core.context_window.errors import (
    CompactionError,
    ContextWindowError,
    TokenCountError,
)


# ---------------------------------------------------------------------------
# ContextWindowSettings
# ---------------------------------------------------------------------------


class TestContextWindowSettings:
    """Tests for ContextWindowSettings model."""

    def test_defaults(self) -> None:
        settings = ContextWindowSettings()
        assert settings.max_context_tokens == 128_000
        assert settings.summary_trigger_ratio == 0.8
        assert settings.compaction_strategy == "observation_masking"
        assert settings.preserve_recent_messages_count == 4
        assert settings.archive_originals is False

    def test_custom_values(self) -> None:
        settings = ContextWindowSettings(
            max_context_tokens=64_000,
            summary_trigger_ratio=0.6,
            compaction_strategy="truncation",
            preserve_recent_messages_count=10,
            archive_originals=True,
        )
        assert settings.max_context_tokens == 64_000
        assert settings.summary_trigger_ratio == 0.6
        assert settings.compaction_strategy == "truncation"
        assert settings.preserve_recent_messages_count == 10
        assert settings.archive_originals is True

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            ContextWindowSettings(unknown_field="oops")

    def test_trigger_ratio_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ContextWindowSettings(summary_trigger_ratio=0.0)

    def test_trigger_ratio_must_be_at_most_one(self) -> None:
        with pytest.raises(ValidationError):
            ContextWindowSettings(summary_trigger_ratio=1.1)

    def test_trigger_ratio_exactly_one_is_valid(self) -> None:
        settings = ContextWindowSettings(summary_trigger_ratio=1.0)
        assert settings.summary_trigger_ratio == 1.0

    def test_max_context_tokens_zero_means_disabled(self) -> None:
        settings = ContextWindowSettings(max_context_tokens=0)
        assert settings.max_context_tokens == 0

    def test_max_context_tokens_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ContextWindowSettings(max_context_tokens=-1)


# ---------------------------------------------------------------------------
# CompactionResult
# ---------------------------------------------------------------------------


class TestCompactionResult:
    """Tests for CompactionResult model."""

    def test_all_fields(self) -> None:
        result = CompactionResult(
            messages=[{"role": "user", "content": "hi"}],
            original_count=10,
            compacted_count=3,
            tokens_before=5000,
            tokens_after=1000,
            strategy_name="truncation",
        )
        assert result.original_count == 10
        assert result.compacted_count == 3
        assert result.tokens_before == 5000
        assert result.tokens_after == 1000
        assert result.strategy_name == "truncation"
        assert result.archived_messages is None

    def test_archived_messages(self) -> None:
        dropped = [{"role": "user", "content": "old"}]
        result = CompactionResult(
            messages=[{"role": "user", "content": "new"}],
            original_count=2,
            compacted_count=1,
            tokens_before=100,
            tokens_after=50,
            strategy_name="truncation",
            archived_messages=dropped,
        )
        assert result.archived_messages == dropped

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            CompactionResult(
                messages=[],
                original_count=0,
                compacted_count=0,
                tokens_before=0,
                tokens_after=0,
                strategy_name="test",
                bad_field=True,
            )


# ---------------------------------------------------------------------------
# CompactionState
# ---------------------------------------------------------------------------


class TestCompactionState:
    """Tests for CompactionState model."""

    def test_defaults(self) -> None:
        state = CompactionState(accumulated_tokens=500, max_tokens=128_000)
        assert state.accumulated_tokens == 500
        assert state.max_tokens == 128_000
        assert state.compaction_count == 0
        assert state.last_compaction_strategy is None

    def test_with_compaction_history(self) -> None:
        state = CompactionState(
            accumulated_tokens=1000,
            max_tokens=128_000,
            compaction_count=3,
            last_compaction_strategy="observation_masking",
        )
        assert state.compaction_count == 3
        assert state.last_compaction_strategy == "observation_masking"

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            CompactionState(
                accumulated_tokens=0,
                max_tokens=0,
                extra_field="nope",
            )


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    """Tests for the context window error hierarchy."""

    def test_context_window_error_is_exception(self) -> None:
        assert issubclass(ContextWindowError, Exception)

    def test_compaction_error_inherits(self) -> None:
        assert issubclass(CompactionError, ContextWindowError)

    def test_token_count_error_inherits(self) -> None:
        assert issubclass(TokenCountError, ContextWindowError)

    def test_catch_base_catches_compaction(self) -> None:
        with pytest.raises(ContextWindowError):
            raise CompactionError("compaction failed")

    def test_catch_base_catches_token_count(self) -> None:
        with pytest.raises(ContextWindowError):
            raise TokenCountError("counting failed")
