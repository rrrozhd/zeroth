"""Allowlisted bounded Playwright producer for the two refresh scenarios."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .coordinator import ActionRecorder

RefreshScenario = Literal["w2_refresh_restoration", "w3_refresh_before_approval"]
_TITLES = {
    "w2_refresh_restoration": "w2_refresh_restoration has exact fail-closed evidence",
    "w3_refresh_before_approval": "w3_refresh_before_approval has exact fail-closed evidence",
}


@dataclass(frozen=True, slots=True)
class BrowserRefreshEvidence:
    scenario_id: RefreshScenario
    run_id: str
    correlation: dict[str, str]
    before_refresh_run_id: str
    restored_run_id: str
    keyboard_focus: tuple[dict[str, object], ...]
    evidence: tuple[str, ...]
    approval_id_before: str | None = None
    approval_id_after: str | None = None
    approval_state_before: str | None = None
    approval_state_after: str | None = None


class BoundedRefreshEvidenceProducer:
    """Run one fixed Playwright spec with no caller-controlled argv."""

    def __init__(
        self,
        *,
        frontend_root: Path,
        environment: dict[str, str],
        timeout_seconds: int = 120,
    ) -> None:
        self.frontend_root = frontend_root.resolve(strict=True)
        if (
            self.frontend_root.name != "frontend"
            or not (self.frontend_root / "e2e" / "negative-resilience.spec.ts").is_file()
        ):
            raise ValueError("browser producer requires the repository frontend root")
        if not 10 <= timeout_seconds <= 180:
            raise ValueError("browser producer timeout must be bounded")
        self.environment = dict(environment)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _attachment(report: object, name: str) -> dict[str, object]:
        matches: list[dict[str, object]] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("name") == name and isinstance(value.get("body"), str):
                    try:
                        decoded = base64.b64decode(value["body"], validate=True)
                        payload = json.loads(decoded)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError("Playwright attachment is malformed") from exc
                    if isinstance(payload, dict):
                        matches.append(payload)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(report)
        if len(matches) != 1:
            raise RuntimeError(f"Playwright report requires exactly one {name} attachment")
        return matches[0]

    def run(
        self,
        scenario_id: RefreshScenario,
        *,
        recorder: ActionRecorder,
    ) -> BrowserRefreshEvidence:
        title = _TITLES[scenario_id]
        argv = (
            "npm",
            "exec",
            "playwright",
            "test",
            "e2e/negative-resilience.spec.ts",
            "--project=desktop-1440",
            "--grep",
            f"^{title}$",
            "--reporter=json",
        )
        gate = f"ZEROTH_EVALUATION_SCENARIO_{scenario_id.upper()}"
        allowed = {
            "PATH",
            "HOME",
            "TMPDIR",
            "ZEROTH_EVALUATION_LIVE",
            "ZEROTH_EVALUATION_API_BASE",
            "ZEROTH_EVALUATION_TENANT",
            "ZEROTH_EVALUATION_API_KEY",
            "ZEROTH_EVALUATION_FAULT_CONTROLLER_URL",
            "ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY",
            "ZEROTH_EVALUATION_ALLOW_NEGATIVE_RUNS",
            "ZEROTH_EVALUATION_WORKFLOW2_ID",
            "ZEROTH_EVALUATION_WORKFLOW2_GRAPH_VERSION",
            "ZEROTH_EVALUATION_WORKFLOW2_DEPLOYMENT_REF",
            "ZEROTH_EVALUATION_WORKFLOW3_ID",
            "ZEROTH_EVALUATION_WORKFLOW3_GRAPH_VERSION",
            "ZEROTH_EVALUATION_WORKFLOW3_DEPLOYMENT_REF",
        }
        child_env = {
            name: value
            for name, value in {**os.environ, **self.environment}.items()
            if name in allowed
        }
        child_env.update(
            {
                "ZEROTH_EVALUATION_LIVE": "1",
                "ZEROTH_EVALUATION_ALLOW_NEGATIVE_RUNS": ("I_ACKNOWLEDGE_BOUNDED_NEGATIVE_RUNS"),
                gate: "1",
            }
        )
        completed = subprocess.run(
            argv,
            cwd=self.frontend_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        command_evidence = recorder.record_command_result(
            name=f"playwright-{scenario_id}",
            argv=argv,
            working_directory=self.frontend_root,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            raise RuntimeError("bounded Playwright refresh scenario failed")
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Playwright JSON reporter output is malformed") from exc
        matching_results: list[dict[str, object]] = []

        def collect_results(value: object, *, matched: bool = False) -> None:
            if isinstance(value, dict):
                matched = matched or value.get("title") == title
                if matched and isinstance(value.get("results"), list):
                    matching_results.extend(
                        item for item in value["results"] if isinstance(item, dict)
                    )
                for child in value.values():
                    collect_results(child, matched=matched)
            elif isinstance(value, list):
                for child in value:
                    collect_results(child, matched=matched)

        collect_results(report)
        if len(matching_results) != 1 or matching_results[0].get("status") != "passed":
            raise RuntimeError("Playwright reporter did not contain one passing target test")
        verification = self._attachment(report, "scenario-verification")
        keyboard = self._attachment(report, "keyboard-focus-order")
        if verification.get("scenario_id") != scenario_id:
            raise RuntimeError("Playwright verification scenario identity mismatch")
        raw_correlation = verification.get("identity")
        if not isinstance(raw_correlation, dict):
            raise RuntimeError("Playwright verification lacks exact correlation identities")
        correlation: dict[str, str] = {}
        for key, value in raw_correlation.items():
            if not isinstance(key, str):
                raise RuntimeError(
                    "Playwright verification lacks exact correlation identities"
                )
            values = value if isinstance(value, list) else [value]
            if (
                len(values) != 1
                or not isinstance(values[0], str)
                or not values[0]
            ):
                raise RuntimeError(
                    "Playwright verification lacks exact correlation identities"
                )
            correlation[key] = values[0]
        run_id = correlation.get("run_id")
        entries = keyboard.get("entries")
        if entries is None and isinstance(keyboard.get("keyboard_focus"), list):
            entries = keyboard["keyboard_focus"]
        if not isinstance(run_id, str) or not isinstance(entries, list) or not entries:
            raise RuntimeError("Playwright refresh evidence is incomplete")
        if not all(
            isinstance(entry, dict) and entry.get("focus_visible") is True for entry in entries
        ):
            raise RuntimeError("Playwright refresh keyboard focus evidence failed")
        restoration = self._attachment(report, "refresh-restoration")
        before = restoration.get("before")
        after = restoration.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise RuntimeError("Playwright refresh restoration evidence is malformed")
        before_refresh_run_id = before.get("run_id")
        restored_run_id = after.get("run_id")
        before_ui_run_id = before.get("ui_run_id")
        restored_ui_run_id = after.get("ui_run_id")
        if (
            not isinstance(run_id, str)
            or not all(
                isinstance(value, str) and value
                for value in (
                    before_refresh_run_id,
                    restored_run_id,
                    before_ui_run_id,
                    restored_ui_run_id,
                )
            )
            or len(
                {
                    run_id,
                    before_refresh_run_id,
                    restored_run_id,
                    before_ui_run_id,
                    restored_ui_run_id,
                }
            )
            != 1
        ):
            raise RuntimeError("Playwright run identity was not restored")
        approval_fields: dict[str, str | None] = {
            "approval_id_before": None,
            "approval_id_after": None,
            "approval_state_before": None,
            "approval_state_after": None,
        }
        if scenario_id == "w3_refresh_before_approval":
            approval_fields = {
                "approval_id_before": before.get("approval_id"),
                "approval_id_after": after.get("approval_id"),
                "approval_state_before": before.get("approval_state"),
                "approval_state_after": after.get("approval_state"),
            }
            if (
                not all(isinstance(value, str) and value for value in approval_fields.values())
                or approval_fields["approval_id_before"] != approval_fields["approval_id_after"]
                or approval_fields["approval_state_before"] != "pending"
                or approval_fields["approval_state_after"] != "pending"
            ):
                raise RuntimeError("Playwright approval identity was not restored")
        return BrowserRefreshEvidence(
            scenario_id=scenario_id,
            run_id=run_id,
            correlation=dict(correlation),
            before_refresh_run_id=before_refresh_run_id,
            restored_run_id=restored_run_id,
            keyboard_focus=tuple(entries),
            evidence=(command_evidence,),
            **approval_fields,
        )
