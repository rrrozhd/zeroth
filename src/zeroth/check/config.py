"""Strict loader for the separate zeroth-check.yaml boundary."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TapeConfig(_StrictConfig):
    """Curated tape location; a relative ``curated_dir`` resolves against the config file."""

    curated_dir: Path

    @field_validator("curated_dir", mode="before")
    @classmethod
    def _path_from_yaml(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value)
        return value


class ReplayConfig(_StrictConfig):
    """Replay policy: exactly three runs with a quorum of two, the only accepted values."""

    runs: Literal[3]
    quorum: Literal[2]


class FaultConfig(_StrictConfig):
    """Fault policy: all mandatory faults always run, plus known ``additional`` faults."""

    required: Literal["all"]
    additional: list[str] = Field(default_factory=list)

    @field_validator("additional")
    @classmethod
    def _known_addons_only(cls, value: list[str]) -> list[str]:
        from zeroth.check.faults.catalog import validate_additional

        validate_additional(value)
        return value


class ReportingConfig(_StrictConfig):
    """Verdict classes that fail the check; each of canary, block, invalid at most once."""

    fail_on: list[Literal["canary", "block", "invalid"]]

    @model_validator(mode="after")
    def _unique_failures(self) -> Self:
        if len(self.fail_on) != len(set(self.fail_on)):
            raise ValueError("fail_on entries must be unique")
        return self


class CheckConfig(_StrictConfig):
    """A validated ``check.v1`` file: target entrypoint plus tape, replay, fault, report policy."""

    version: Literal["check.v1"]
    target: str
    tapes: TapeConfig
    replay: ReplayConfig
    faults: FaultConfig
    reporting: ReportingConfig

    @field_validator("target")
    @classmethod
    def _build_target_entrypoint(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:build_target", value):
            raise ValueError("target must be an importable module:build_target entrypoint")
        return value


def load_check_config(path: str | Path = "zeroth-check.yaml") -> CheckConfig:
    """Load and strictly validate a check config, anchoring a relative tape dir to the file."""
    config_path = Path(path).resolve()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = CheckConfig.model_validate(loaded)
    tapes = config.tapes
    if not tapes.curated_dir.is_absolute():
        tapes = tapes.model_copy(update={"curated_dir": config_path.parent / tapes.curated_dir})
        config = config.model_copy(update={"tapes": tapes})
    return config
