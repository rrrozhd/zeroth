"""ZER-37: a hostile checkout's ``zeroth.yaml`` cannot become platform settings.

:class:`ZerothSettings` reads ``zeroth.yaml`` through a YAML source whose path
used to be resolved against the *current* working directory on every
construction, with ``extra="ignore"`` hiding the absorption: a zeroth process
whose CWD later moved inside a hostile repository checkout silently absorbed
that repo's ``zeroth.yaml`` as platform configuration (sandbox backend flips
included). The hardening pins the path once at settings-module import, so only
the launch environment -- the launch CWD or ``ZEROTH_SETTINGS_FILE`` -- decides
which file is read.

Covered here:

* in-process: an ``os.chdir`` into a hostile checkout AFTER import is inert
  for a freshly constructed ``ZerothSettings`` (not the cached singleton);
* subprocess: ``ZEROTH_SETTINGS_FILE`` wins over a hostile CWD at process
  start, and -- the honest inverse control -- a process legitimately launched
  inside its configuration directory still reads that directory's file;
* belt: nothing under the GitHub integration or the repository-run service
  calls ``os.chdir`` at all, so the in-process hazard has no trigger in the
  ZER-37 code paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zeroth.platform.config.settings import _SETTINGS_YAML_PATH, ZerothSettings

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOSTILE_YAML = """\
sandbox:
  backend: docker
redis:
  key_prefix: hostile-canary-prefix
"""

_BENIGN_YAML = """\
redis:
  key_prefix: benign-operator-prefix
"""

_PROBE = """\
import json
from zeroth.platform.config.settings import _SETTINGS_YAML_PATH, ZerothSettings
settings = ZerothSettings()
print(json.dumps({
    "sandbox_backend": settings.sandbox.backend,
    "redis_key_prefix": settings.redis.key_prefix,
    "yaml_path": str(_SETTINGS_YAML_PATH),
}))
"""


def _hostile_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "hostile-checkout"
    checkout.mkdir()
    (checkout / "zeroth.yaml").write_text(_HOSTILE_YAML, encoding="utf-8")
    (checkout / ".env").write_text(
        'ZEROTH_REDIS__KEY_PREFIX="hostile-dotenv-prefix"\n', encoding="utf-8"
    )
    return checkout


def _spawn_probe(cwd: Path, extra_env: dict[str, str]) -> dict[str, str]:
    """Run the settings probe in a fresh interpreter and decode its report."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("ZEROTH_")}
    env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"settings probe failed:\n{result.stderr}"
    return json.loads(result.stdout)


def test_chdir_into_a_hostile_checkout_cannot_retarget_the_yaml_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import-time pin makes a post-import chdir irrelevant in-process."""
    monkeypatch.delenv("ZEROTH_SANDBOX__BACKEND", raising=False)
    monkeypatch.delenv("ZEROTH_REDIS__KEY_PREFIX", raising=False)
    hostile = _hostile_checkout(tmp_path)
    monkeypatch.chdir(hostile)

    # A FRESH construction, not the cached get_settings() singleton: even a
    # settings object built while the CWD sits inside the hostile checkout
    # reads the path pinned when the module was imported at session start.
    settings = ZerothSettings()

    assert settings.sandbox.backend == "local"
    # Covers BOTH absorption channels: the hostile zeroth.yaml flips the
    # sandbox backend, the hostile .env flips the redis key prefix — a fresh
    # construction after chdir must see neither.
    assert settings.redis.key_prefix == "zeroth"
    # The pin itself never pointed into the hostile tree.
    assert _SETTINGS_YAML_PATH.is_absolute()
    assert hostile not in _SETTINGS_YAML_PATH.parents


def test_settings_file_env_vars_beat_a_hostile_launch_cwd(tmp_path: Path) -> None:
    """A fresh process started IN the hostile dir obeys BOTH path overrides.

    ZEROTH_SETTINGS_FILE pins the YAML source and ZEROTH_ENV_FILE pins the
    dotenv source; with both set, a hostile launch CWD contributes nothing.
    Setting only one leaves the other file's absorption channel open -- the
    overrides are a pair, which the deployment docs state.
    """
    hostile = _hostile_checkout(tmp_path)
    benign = tmp_path / "operator" / "zeroth.yaml"
    benign.parent.mkdir()
    benign.write_text(_BENIGN_YAML, encoding="utf-8")
    benign_env = tmp_path / "operator" / ".env"
    benign_env.write_text("", encoding="utf-8")

    report = _spawn_probe(
        hostile,
        {"ZEROTH_SETTINGS_FILE": str(benign), "ZEROTH_ENV_FILE": str(benign_env)},
    )

    assert report["sandbox_backend"] == "local"
    assert report["redis_key_prefix"] == "benign-operator-prefix"
    assert report["yaml_path"] == str(benign.resolve())


def test_process_launched_in_its_config_directory_reads_it(tmp_path: Path) -> None:
    """Inverse control, documenting the operator contract honestly: without
    the path overrides, the launch CWD's zeroth.yaml AND .env are the
    configuration -- starting a zeroth process inside an untrusted tree
    remains an operator error, exactly like starting it with a hostile
    config file. The .env value winning over the YAML value also pins the
    documented env > .env > YAML precedence."""
    hostile = _hostile_checkout(tmp_path)

    report = _spawn_probe(hostile, {})

    assert report["sandbox_backend"] == "docker"
    assert report["redis_key_prefix"] == "hostile-dotenv-prefix"
    assert report["yaml_path"] == str((hostile / "zeroth.yaml").resolve())


def test_no_github_or_repository_code_calls_chdir() -> None:
    """Belt: the ZER-37 code paths never move the process CWD themselves."""
    roots = (
        _REPO_ROOT / "src" / "zeroth" / "integrations" / "github",
        _REPO_ROOT / "src" / "zeroth" / "service" / "repositories",
    )
    scanned: list[Path] = []
    offenders: list[str] = []
    for root in roots:
        assert root.is_dir(), f"expected source tree missing: {root}"
        for path in sorted(root.rglob("*.py")):
            scanned.append(path)
            text = path.read_text(encoding="utf-8")
            if "os.chdir" in text or "chdir(" in text:
                offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert scanned, "belt test scanned no files -- source layout moved?"
    assert offenders == []
