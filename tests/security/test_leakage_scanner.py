from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from release.security.scan import CredentialLeakScanner, scan_paths


CANARY = 's3cr3t/+ "with\\spaces"'


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.parametrize(
    ("rule", "encoded"),
    [
        ("canary:exact", CANARY),
        ("canary:url", quote(CANARY, safe="")),
        ("canary:url", quote_plus(CANARY, safe="")),
        ("canary:json", json.dumps(CANARY)[1:-1]),
        ("canary:base64", base64.b64encode(CANARY.encode()).decode()),
        ("canary:base64", base64.urlsafe_b64encode(CANARY.encode()).decode()),
        ("canary:hex", CANARY.encode().hex()),
    ],
)
def test_scanner_detects_canary_encodings_without_exposing_them(rule: str, encoded: str) -> None:
    scanner = CredentialLeakScanner([CANARY])

    findings = scanner.scan(f"prefix:{encoded}:suffix", surface="api-response")

    assert [finding.as_dict() for finding in findings] == [
        {
            "fingerprint": _fingerprint(CANARY),
            "rule": rule,
            "surface": "api-response",
        }
    ]
    diagnostics = json.dumps([finding.as_dict() for finding in findings], sort_keys=True)
    assert CANARY not in diagnostics
    assert encoded not in diagnostics


def test_scanner_detects_bytes_nested_values_and_github_token_patterns() -> None:
    github_token = "ghp_" + "A" * 36
    scanner = CredentialLeakScanner([CANARY])
    value = {
        "safe-key": [b"prefix " + CANARY.encode(), {"token": github_token}],
        b"binary-key": (memoryview(CANARY.encode()),),
    }

    findings = scanner.scan(value, surface="audit")

    assert [item.as_dict() for item in findings] == [
        {
            "fingerprint": _fingerprint(CANARY),
            "rule": "canary:exact",
            "surface": "audit",
        },
        {
            "fingerprint": _fingerprint(github_token),
            "rule": "github:classic",
            "surface": "audit",
        },
    ]


def test_scanner_supports_non_utf8_byte_canaries() -> None:
    canary = b"\xff\x00binary-secret"
    scanner = CredentialLeakScanner([canary])

    findings = scanner.scan(base64.b64encode(canary), surface="binary")

    assert [(item.rule, item.fingerprint) for item in findings] == [
        ("canary:base64", "sha256:" + hashlib.sha256(canary).hexdigest())
    ]


@pytest.mark.parametrize(
    "encoded",
    [
        base64.b64encode(CANARY.encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(CANARY.encode()).decode().rstrip("="),
    ],
)
def test_scanner_detects_unpadded_base64(encoded: str) -> None:
    findings = CredentialLeakScanner([CANARY]).scan(encoded, surface="output")

    assert [item.rule for item in findings] == ["canary:base64"]


def test_scanner_detects_uppercase_hex_and_lowercase_percent_escapes() -> None:
    scanner = CredentialLeakScanner([CANARY])

    hex_findings = scanner.scan(CANARY.encode().hex().upper(), surface="hex")
    url_findings = scanner.scan(quote(CANARY, safe="").lower(), surface="url")

    assert [item.rule for item in hex_findings] == ["canary:hex"]
    assert [item.rule for item in url_findings] == ["canary:url"]


@pytest.mark.parametrize(
    ("rule", "encoded"),
    [
        ("canary:hex", "41622B2f4364"),
        ("canary:url", "Ab%2b%2FCd"),
        ("canary:json", r"Ab+\/Cd"),
    ],
)
def test_representation_aware_matching_and_surface_safety(rule: str, encoded: str) -> None:
    canary = "Ab+/Cd"
    scanner = CredentialLeakScanner([canary])

    findings = scanner.scan(encoded, surface=f"surface-{encoded}")

    assert [(item.rule, item.fingerprint) for item in findings] == [
        (rule, _fingerprint(canary))
    ]
    assert encoded not in findings[0].surface
    assert findings[0].surface.startswith("surface:sha256:")


def test_scan_paths_detects_binary_value_split_across_read_chunks(tmp_path: Path) -> None:
    canary = "chunk-boundary-secret"
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"x" * 65_530 + canary.encode() + b"tail")

    findings = scan_paths(tmp_path, [target], canaries=[canary])

    assert [item.rule for item in findings] == ["canary:exact"]
    assert findings[0].surface == "evidence.bin"


@pytest.mark.parametrize("target_location", ["inside", "outside"])
def test_scan_paths_rejects_symlink_file_without_following_it(
    tmp_path: Path, target_location: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target_root = root if target_location == "inside" else tmp_path
    target = target_root / "target.txt"
    target.write_text(CANARY)
    link = root / f"secret-link-{CANARY.encode().hex()}"
    link.symlink_to(target)

    with pytest.raises(ValueError) as excinfo:
        scan_paths(root, [link], canaries=[CANARY])

    assert CANARY not in str(excinfo.value)


@pytest.mark.parametrize("target_location", ["inside", "outside"])
def test_scan_paths_rejects_symlink_directory_component_without_following_it(
    tmp_path: Path, target_location: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target_root = root if target_location == "inside" else tmp_path
    target = target_root / "target-directory"
    target.mkdir()
    (target / "evidence.txt").write_text(CANARY)
    link = root / "linked-directory"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        scan_paths(root, [link / "evidence.txt"], canaries=[CANARY])


def test_scan_paths_rejects_parent_components_before_path_normalization(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "evidence.txt").write_text("safe")

    with pytest.raises(ValueError):
        scan_paths(root, [root / "unused" / ".." / "evidence.txt"], canaries=[CANARY])


def test_scan_paths_rejects_symlink_in_lexical_root_ancestors(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    root = actual / "root"
    root.mkdir(parents=True)
    (root / "evidence.txt").write_text("safe")
    encoded = CANARY.encode().hex()
    alias = tmp_path / f"alias-{encoded}"
    alias.symlink_to(actual, target_is_directory=True)
    supplied_root = alias / "root"

    with pytest.raises(ValueError) as excinfo:
        scan_paths(supplied_root, [Path("evidence.txt")], canaries=[CANARY])

    assert encoded not in str(excinfo.value)


def test_scan_paths_rejects_parent_components_in_supplied_root(tmp_path: Path) -> None:
    root = tmp_path / "actual" / "root"
    root.mkdir(parents=True)
    (root / "evidence.txt").write_text("safe")
    supplied_root = tmp_path / "actual" / "unused" / ".." / "root"

    with pytest.raises(ValueError):
        scan_paths(supplied_root, [Path("evidence.txt")], canaries=[CANARY])


def test_findings_are_deduplicated_and_deterministically_sorted() -> None:
    scanner = CredentialLeakScanner(["z-secret", "a-secret"])

    findings = scanner.scan(
        {"z": ["z-secret", "z-secret"], "a": "a-secret"}, surface="surface"
    )

    assert findings == scanner.scan(
        {"a": "a-secret", "z": ["z-secret", "z-secret"]}, surface="surface"
    )
    assert len(findings) == 2


def test_secret_bearing_surface_label_is_fingerprinted_not_echoed() -> None:
    findings = CredentialLeakScanner([CANARY]).scan(
        CANARY, surface=f"api-response-{CANARY}"
    )

    diagnostic = json.dumps(findings[0].as_dict())
    assert CANARY not in findings[0].surface
    assert CANARY not in diagnostic
    assert findings[0].surface.startswith("surface:sha256:")


def _run_cli(root: Path, *paths: Path, canary: str = CANARY) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).parents[2] / "release" / "security" / "scan.py"
    environment = dict(os.environ)
    environment["ZEROTH_SECURITY_CANARIES"] = json.dumps([canary])
    return subprocess.run(
        [sys.executable, str(script), "--root", str(root), *(str(path) for path in paths)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_cli_writes_deterministic_json_and_exits_nonzero_on_findings(tmp_path: Path) -> None:
    first = tmp_path / "b.txt"
    second = tmp_path / "a.txt"
    first.write_text(f"noise {CANARY}")
    second.write_text("safe")

    completed = _run_cli(tmp_path, first, second)

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "findings": [
            {
                "fingerprint": _fingerprint(CANARY),
                "rule": "canary:exact",
                "surface": "b.txt",
            }
        ],
        "status": "failed",
    }
    assert completed.stdout == json.dumps(json.loads(completed.stdout), sort_keys=True) + "\n"
    assert CANARY not in completed.stdout


def test_cli_resolves_relative_inputs_under_the_declared_root(tmp_path: Path) -> None:
    target = tmp_path / "relative.txt"
    target.write_text(CANARY)

    completed = _run_cli(tmp_path, Path("relative.txt"))

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["findings"][0]["surface"] == "relative.txt"


def test_cli_does_not_echo_secret_bearing_file_name(tmp_path: Path) -> None:
    file_canary = "file-secret-token"
    target = tmp_path / f"evidence-{file_canary}"
    target.write_text(file_canary)

    completed = _run_cli(tmp_path, target, canary=file_canary)

    assert completed.returncode == 1
    assert file_canary not in completed.stdout
    assert json.loads(completed.stdout)["findings"][0]["surface"].startswith(
        "surface:sha256:"
    )


@pytest.mark.parametrize(
    ("rule", "encoded"),
    [
        ("canary:hex", "41622B2f4364"),
        ("canary:url", "Ab%2b%2FCd"),
        ("canary:json", r"Ab+\/Cd"),
    ],
)
def test_cli_does_not_echo_encoded_secret_in_path_components(
    tmp_path: Path, rule: str, encoded: str
) -> None:
    canary = "Ab+/Cd"
    target = tmp_path / f"evidence-{encoded}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded)

    completed = _run_cli(tmp_path, target, canary=canary)

    assert completed.returncode == 1
    assert encoded not in completed.stdout
    finding = json.loads(completed.stdout)["findings"][0]
    assert finding["surface"].startswith("surface:sha256:")
    assert finding["rule"] == rule
    assert finding["fingerprint"] == _fingerprint(canary)


def test_cli_argument_errors_never_echo_supplied_values() -> None:
    script = Path(__file__).parents[2] / "release" / "security" / "scan.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--unknown", CANARY],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert CANARY not in completed.stdout
    assert set(json.loads(completed.stdout)["diagnostics"][0]) == {
        "fingerprint",
        "rule",
        "surface",
    }


@pytest.mark.parametrize("failure", ["missing", "outside"])
def test_cli_fails_closed_with_safe_diagnostic_for_invalid_required_input(
    tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "missing.txt" if failure == "missing" else tmp_path / "outside.txt"
    if failure == "outside":
        path.write_text(CANARY)

    completed = _run_cli(root, path)

    assert completed.returncode == 2
    assert completed.stderr == ""
    report = json.loads(completed.stdout)
    assert report["status"] == "error"
    assert list(report) == ["diagnostics", "findings", "status"]
    assert report["findings"] == []
    assert len(report["diagnostics"]) == 1
    assert set(report["diagnostics"][0]) == {"fingerprint", "rule", "surface"}
    assert CANARY not in completed.stdout


def _run_module_cli(
    cwd: Path, *arguments: str, canary: str | None = None
) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)
    if canary is not None:
        environment["ZEROTH_SECURITY_CANARIES"] = json.dumps([canary])
    else:
        environment.pop("ZEROTH_SECURITY_CANARIES", None)
    return subprocess.run(
        [sys.executable, "-m", "release.security.scan", *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


def test_module_cli_scans_directory_and_atomically_writes_requested_report(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "release" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "result.json").write_text('{"status":"passed"}')
    output = evidence / "security-scan.json"

    completed = _run_module_cli(
        tmp_path,
        "release/evidence",
        "--output",
        "release/evidence/security-scan.json",
    )

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert json.loads(output.read_text()) == {"findings": [], "status": "passed"}
    assert output.read_text() == json.dumps(json.loads(output.read_text()), sort_keys=True) + "\n"
    assert not list(evidence.glob(".*security-scan.json.*"))


def test_module_cli_writes_report_and_returns_nonzero_on_finding(tmp_path: Path) -> None:
    evidence = tmp_path / "release" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "result.txt").write_text(CANARY)
    output = evidence / "security-scan.json"

    completed = _run_module_cli(
        tmp_path,
        "release/evidence",
        "--output",
        "release/evidence/security-scan.json",
        canary=CANARY,
    )

    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == ""
    assert json.loads(output.read_text())["status"] == "failed"
    assert CANARY not in output.read_text()


def test_module_cli_writes_error_report_for_unreadable_input(tmp_path: Path) -> None:
    (tmp_path / "release" / "evidence").mkdir(parents=True)
    output = tmp_path / "release" / "evidence" / "security-scan.json"

    completed = _run_module_cli(
        tmp_path,
        "release/missing",
        "--output",
        "release/evidence/security-scan.json",
    )

    assert completed.returncode == 2
    assert completed.stdout == completed.stderr == ""
    assert json.loads(output.read_text())["status"] == "error"


def test_module_cli_rejects_output_outside_root_without_writing_it(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path.parent / f"outside-{tmp_path.name}.json"

    completed = _run_module_cli(
        tmp_path, "evidence", "--output", str(outside)
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert not outside.exists()


def test_module_cli_does_not_write_through_a_symlink_scan_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    (actual / "evidence").mkdir(parents=True)
    (actual / "evidence" / "result.txt").write_text("safe")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    completed = _run_module_cli(
        tmp_path,
        "--root",
        str(alias),
        "evidence",
        "--output",
        "security-scan.json",
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert not (actual / "security-scan.json").exists()


def test_module_cli_does_not_write_through_a_symlink_root_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    root = actual / "root"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "result.txt").write_text("safe")
    encoded = CANARY.encode().hex()
    alias = tmp_path / f"alias-{encoded}"
    alias.symlink_to(actual, target_is_directory=True)
    output = root / "security-scan.json"

    completed = _run_module_cli(
        tmp_path,
        "--root",
        str(alias / "root"),
        "evidence",
        "--output",
        "security-scan.json",
        canary=CANARY,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert encoded not in completed.stdout
    assert not output.exists()


def test_module_cli_rejects_parent_components_in_root_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "actual" / "root"
    (root / "evidence").mkdir(parents=True)
    supplied_root = tmp_path / "actual" / "unused" / ".." / "root"

    completed = _run_module_cli(
        tmp_path,
        "--root",
        str(supplied_root),
        "evidence",
        "--output",
        "security-scan.json",
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert not (root / "security-scan.json").exists()
