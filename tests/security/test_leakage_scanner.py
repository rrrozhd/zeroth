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

from release.security import scan as scan_module
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
        ("canary:hex", "41622B2f43642139"),
        ("canary:url", "Ab%2b%2FCd%219"),
        ("canary:json", r"Ab+\/Cd!9"),
    ],
)
def test_representation_aware_matching_and_surface_safety(rule: str, encoded: str) -> None:
    canary = "Ab+/Cd!9"
    scanner = CredentialLeakScanner([canary])

    findings = scanner.scan(encoded, surface=f"surface-{encoded}")

    assert [(item.rule, item.fingerprint) for item in findings] == [(rule, _fingerprint(canary))]
    assert encoded not in findings[0].surface
    assert findings[0].surface.startswith("surface:sha256:")


def test_scan_paths_detects_binary_value_split_across_read_chunks(tmp_path: Path) -> None:
    canary = "chunk-boundary-secret"
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"x" * 65_530 + canary.encode() + b"tail")

    findings = scan_paths(tmp_path, [target], canaries=[canary])

    assert [item.rule for item in findings] == ["canary:exact"]
    assert findings[0].surface == "evidence.bin"


@pytest.mark.parametrize(
    ("rule", "canary", "encoded"),
    [
        ("canary:url", "secret-73", "%73%65%63%72%65%74%2D%37%33"),
        ("canary:url", "secret-73", "sec%72et%2d73"),
        ("canary:json", "café-token", r"caf\u00E9-token"),
        ("canary:json", "café-token", r"caf\u00e9-token"),
        ("canary:json", "token-😀", r"token-\uD83D\uDE00"),
    ],
)
def test_semantic_encoding_normalization(rule: str, canary: str, encoded: str) -> None:
    findings = CredentialLeakScanner([canary]).scan(encoded, surface=f"label-{encoded}")

    assert [(item.rule, item.fingerprint) for item in findings] == [(rule, _fingerprint(canary))]
    assert encoded not in findings[0].surface


@pytest.mark.parametrize("malformed", [b"%", b"%GG", b"\\u12", b"\\uZZZZ", b"\xff%2"])
def test_malformed_encodings_do_not_crash(malformed: bytes) -> None:
    assert CredentialLeakScanner([CANARY]).scan(malformed, surface="malformed") == []


def test_semantic_encoding_split_across_file_chunks(tmp_path: Path) -> None:
    canary = "café-token"
    encoded = rb"caf\u00E9-token"
    target = tmp_path / "evidence.bin"
    target.write_bytes(b"x" * 65_532 + encoded)

    findings = scan_paths(tmp_path, [target], canaries=[canary])

    assert [item.rule for item in findings] == ["canary:json"]


@pytest.mark.parametrize("canary", ["short", "aaaaaaaa", b"1234567", b"abcd" * 5000])
def test_weak_canaries_are_rejected_without_echoing_them(canary: str | bytes) -> None:
    with pytest.raises(ValueError) as excinfo:
        CredentialLeakScanner([canary])

    rendered = str(excinfo.value)
    value = canary.decode() if isinstance(canary, bytes) else canary
    assert value not in rendered


def test_cli_weak_canary_is_a_structured_error_report(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("safe")
    output = tmp_path / "scan.json"

    completed = _run_module_cli(tmp_path, "evidence", "--output", "scan.json", canary="short")

    assert completed.returncode == 2
    assert completed.stdout == completed.stderr == ""
    assert json.loads(output.read_text())["status"] == "error"
    assert "short" not in output.read_text()


@pytest.mark.parametrize(
    ("token", "detected"),
    [
        ("ghp_" + "A" * 36, True),
        ("xghp_" + "A" * 36, False),
        ("ghp_" + "A" * 37, False),
        ("github_pat_" + "A" * 82, True),
        ("github_pat_" + "A" * 255, True),
        ("github_pat_" + "A" * 256, False),
        ("xgithub_pat_" + "A" * 82, False),
    ],
)
def test_github_token_patterns_require_declared_lengths_and_boundaries(
    token: str, detected: bool
) -> None:
    findings = CredentialLeakScanner([]).scan(token, surface="token")

    assert bool(findings) is detected


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

    findings = scanner.scan({"z": ["z-secret", "z-secret"], "a": "a-secret"}, surface="surface")

    assert findings == scanner.scan(
        {"a": "a-secret", "z": ["z-secret", "z-secret"]}, surface="surface"
    )
    assert len(findings) == 2


def test_secret_bearing_surface_label_is_fingerprinted_not_echoed() -> None:
    findings = CredentialLeakScanner([CANARY]).scan(CANARY, surface=f"api-response-{CANARY}")

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
    assert json.loads(completed.stdout)["findings"][0]["surface"].startswith("surface:sha256:")


@pytest.mark.parametrize(
    ("rule", "encoded"),
    [
        ("canary:hex", "41622B2f43642139"),
        ("canary:url", "Ab%2b%2FCd%219"),
        ("canary:json", r"Ab+\/Cd!9"),
    ],
)
def test_cli_does_not_echo_encoded_secret_in_path_components(
    tmp_path: Path, rule: str, encoded: str
) -> None:
    canary = "Ab+/Cd!9"
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

    completed = _run_module_cli(tmp_path, "evidence", "--output", str(outside))

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


@pytest.mark.parametrize("_iteration", range(10))
def test_input_parent_swap_cannot_redirect_scan_outside_root(
    tmp_path: Path, monkeypatch, _iteration: int
) -> None:
    root = tmp_path / "root"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "result.txt").write_text("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text(CANARY)
    original_open = os.open
    swapped = False

    def _swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and path == "evidence":
            swapped = True
            evidence.rename(root / "detached")
            evidence.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(scan_module.os, "open", _swap_before_open)

    with pytest.raises(ValueError) as excinfo:
        scan_paths(root, [Path("evidence")], canaries=[CANARY])

    assert CANARY not in str(excinfo.value)


@pytest.mark.parametrize("mutation", ["delete", "directory"])
def test_concurrent_input_mutation_is_a_structured_safe_report(
    tmp_path: Path, monkeypatch, capsys, mutation: str
) -> None:
    root = tmp_path / "root"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    target = evidence / f"secret-{CANARY.encode().hex()}"
    target.write_text("safe")
    reports = root / "reports"
    reports.mkdir()
    original_open = scan_module._open_child_descriptor
    deleted = False

    def _delete_before_open(parent_fd: int, name: str, *, directory: bool) -> int:
        nonlocal deleted
        if not deleted and name == target.name:
            deleted = True
            target.unlink()
            if mutation == "directory":
                target.mkdir()
        return original_open(parent_fd, name, directory=directory)

    monkeypatch.setattr(scan_module, "_open_child_descriptor", _delete_before_open)

    exit_code = scan_module.main(["--root", str(root), "evidence", "--output", "reports/scan.json"])

    captured = capsys.readouterr()
    report = (reports / "scan.json").read_text()
    assert exit_code == 2
    assert captured.out == captured.err == ""
    assert json.loads(report)["status"] == "error"
    assert CANARY not in report
    assert CANARY.encode().hex() not in report


@pytest.mark.parametrize("_iteration", range(10))
def test_output_parent_swap_cannot_redirect_atomic_report(
    tmp_path: Path, monkeypatch, capsys, _iteration: int
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("safe")
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original_create = scan_module._create_temp_descriptor
    swapped = False

    def _swap_before_temp(target):
        nonlocal swapped
        if not swapped:
            swapped = True
            reports.rename(tmp_path / "detached-reports")
            reports.symlink_to(outside, target_is_directory=True)
        return original_create(target)

    monkeypatch.setattr(scan_module, "_create_temp_descriptor", _swap_before_temp)

    exit_code = scan_module.main(
        ["--root", str(tmp_path), "evidence", "--output", "reports/scan.json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == ""
    assert not (outside / "scan.json").exists()


def test_output_cleanup_failure_never_masks_primary_error_or_leaks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "result.txt").write_text("safe")
    reports = tmp_path / "reports"
    reports.mkdir()

    def _replace_failure(*_args, **_kwargs):
        raise OSError(f"replace failed at {CANARY}")

    def _cleanup_failure(*_args, **_kwargs):
        raise OSError(f"cleanup failed at {CANARY}")

    monkeypatch.setattr(scan_module.os, "replace", _replace_failure)
    monkeypatch.setattr(scan_module.os, "unlink", _cleanup_failure)

    exit_code = scan_module.main(
        ["--root", str(tmp_path), "evidence", "--output", "reports/scan.json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == ""
    assert "Traceback" not in captured.out
    assert CANARY not in captured.out
