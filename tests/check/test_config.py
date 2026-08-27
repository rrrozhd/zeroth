from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.check.config import CheckConfig, load_check_config


VALID = """\
version: check.v1
target: examples.check_payment.target:build_target
tapes:
  curated_dir: checks/tapes
replay:
  runs: 3
  quorum: 2
faults:
  required: all
  additional: []
reporting:
  fail_on: [block, invalid]
"""


def test_loads_minimum_strict_config(tmp_path) -> None:
    path = tmp_path / "zeroth-check.yaml"
    path.write_text(VALID)
    config = load_check_config(path)
    assert config.target == "examples.check_payment.target:build_target"
    assert config.tapes.curated_dir == tmp_path / "checks/tapes"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("version: check.v1", "version: check.v2"),
        ("runs: 3", "runs: 4"),
        ("quorum: 2", "quorum: 1"),
        ("fail_on: [block, invalid]", "fail_on: [pass]"),
        ("target: examples.check_payment.target:build_target", "target: bad-target"),
    ],
)
def test_rejects_invalid_constants(old: str, new: str) -> None:
    with pytest.raises(ValidationError):
        CheckConfig.model_validate(__import__("yaml").safe_load(VALID.replace(old, new)))


def test_rejects_unknown_keys_and_python_fault_hooks() -> None:
    with pytest.raises(ValidationError):
        CheckConfig.model_validate(__import__("yaml").safe_load(VALID + "callback: pkg:run\n"))
    with pytest.raises(ValidationError):
        CheckConfig.model_validate(
            __import__("yaml").safe_load(VALID.replace("additional: []", "additional: [pkg:hook]"))
        )
