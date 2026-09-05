"""Release CI must reject missing Docker coverage during collection."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('required', [False, True])
@pytest.mark.parametrize('probe', ['missing', 'failed'])
def test_missing_docker_fails_only_when_required(tmp_path, required, probe):
    if probe == 'failed':
        docker = tmp_path/'docker'
        docker.write_text('#!/bin/sh\nexit 1\n')
        docker.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path), ZEROTH_REQUIRE_DOCKER='1' if required else '0')
    result = subprocess.run(
        [sys.executable, '-c', 'import tests.conftest as c; print(c.requires_docker.args[0])'],
        env=env, capture_output=True, text=True,
    )
    if required:
        assert result.returncode != 0
        assert 'Docker is required' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'True'


def test_required_docker_accepts_successful_probe(tmp_path):
    docker = tmp_path/'docker'
    docker.write_text('#!/bin/sh\nexit 0\n')
    docker.chmod(0o755)
    result = subprocess.run(
        [sys.executable, '-c', 'import tests.conftest as c; print(c.requires_docker.args[0])'],
        env=dict(os.environ, PATH=str(tmp_path), ZEROTH_REQUIRE_DOCKER='1'),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'False'


def test_source_workflow_requires_docker():
    from pathlib import Path
    import yaml

    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root/'.github/workflows/release-gates.yml').read_text())
    assert workflow['jobs']['source']['env']['ZEROTH_REQUIRE_DOCKER'] == '1'
