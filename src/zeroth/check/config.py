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
    curated_dir: Path

    @field_validator("curated_dir", mode="before")
    @classmethod
    def _path_from_yaml(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value)
        return value


class ReplayConfig(_StrictConfig):
    runs: Literal[3]
    quorum: Literal[2]


class FaultConfig(_StrictConfig):
    required: Literal["all"]
    additional: list[str] = Field(default_factory=list)

    @field_validator("additional")
    @classmethod
    def _known_addons_only(cls, value: list[str]) -> list[str]:
        from zeroth.check.faults.catalog import validate_additional

        validate_additional(value)
        return value


class ReportingConfig(_StrictConfig):
    fail_on: list[Literal["canary", "block", "invalid"]]

    @model_validator(mode="after")
    def _unique_failures(self) -> Self:
        if len(self.fail_on) != len(set(self.fail_on)):
            raise ValueError("fail_on entries must be unique")
        return self


class CheckConfig(_StrictConfig):
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
    config_path = Path(path).resolve()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = CheckConfig.model_validate(loaded)
    tapes = config.tapes
    if not tapes.curated_dir.is_absolute():
        tapes = tapes.model_copy(update={"curated_dir": config_path.parent / tapes.curated_dir})
        config = config.model_copy(update={"tapes": tapes})
    return config
