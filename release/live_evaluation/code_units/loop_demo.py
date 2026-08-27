"""Deterministic local transformations used by the manifest-backed loop demos."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable


def _apply_evaluation_delay(data: dict) -> None:
    """Pause only when the bounded local-validation hook is explicitly set."""

    if "evaluation_delay_ms" not in data:
        return
    delay_ms = data["evaluation_delay_ms"]
    if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 8_000:
        raise ValueError("evaluation_delay_ms must be an integer between 0 and 8000")
    time.sleep(delay_ms / 1_000)


def incident_assess(data: dict) -> dict:
    required = ["service", "severity", "owner", "rollback_plan", "communications"]
    missing = [key for key in required if not data.get(key)]
    data["missing_readiness_fields"] = missing
    data["ready"] = not missing
    data["readiness_score"] = round((len(required) - len(missing)) / len(required), 2)
    data.setdefault("planning_round", 0)
    return data


def incident_prepare(data: dict) -> dict:
    blocked_fields = set(data.get("blocked_fields", []))
    defaults = {
        "service": "checkout-api",
        "severity": "SEV-2",
        "owner": "incident-commander",
        "rollback_plan": "Restore the last known-good deployment and verify error rate",
        "communications": "Post updates every 15 minutes in the incident channel",
    }
    for key, value in defaults.items():
        if key not in blocked_fields:
            data.setdefault(key, value)
    data["planning_round"] = int(data.get("planning_round", 0)) + 1
    data["planned_actions"] = [
        "freeze deploys",
        "capture baseline",
        "prepare rollback",
        "notify stakeholders",
    ]
    return data


def incident_finalize(data: dict) -> dict:
    data["result"] = {
        "status": "ready" if data.get("ready") else "escalate",
        "score": data.get("readiness_score"),
        "planning_rounds": data.get("planning_round", 0),
        "missing": data.get("missing_readiness_fields", []),
    }
    return data


def incident_escalate(data: dict) -> dict:
    data["result"] = {
        "status": "escalate",
        "reason": "max_retries_exhausted",
        "planning_rounds": data.get("planning_round", 0),
        "missing": data.get("missing_readiness_fields", []),
    }
    return data


def quality_inspect(data: dict) -> dict:
    _apply_evaluation_delay(data)
    records = list(data.get("records", []))
    issues = []
    for index, record in enumerate(records):
        name = str(record.get("name", ""))
        email = str(record.get("email", ""))
        status = str(record.get("status", ""))
        if name != name.strip() or not name.strip():
            issues.append({"row": index, "field": "name"})
        if email != email.strip().lower() or "@" not in email:
            issues.append({"row": index, "field": "email"})
        if status not in {"active", "inactive", "pending"}:
            issues.append({"row": index, "field": "status"})
    data["quality_issues"] = issues
    data["needs_repair"] = bool(issues)
    data["quality_score"] = (
        1.0 if not records else round(1 - len(issues) / (len(records) * 3), 3)
    )
    data.setdefault("repair_pass", 0)
    return data


def quality_repair(data: dict) -> dict:
    cleaned = []
    for record in list(data.get("records", [])):
        item = dict(record)
        item["name"] = str(item.get("name", "")).strip() or "Unknown customer"
        item["email"] = str(item.get("email", "")).strip().lower()
        if "@" not in item["email"]:
            item["email"] = "unknown@example.invalid"
        if item.get("status") not in {"active", "inactive", "pending"}:
            item["status"] = "pending"
        cleaned.append(item)
    data["records"] = cleaned
    data["repair_pass"] = int(data.get("repair_pass", 0)) + 1
    return data


def quality_finalize(data: dict) -> dict:
    data["result"] = {
        "status": "ready" if not data.get("needs_repair") else "manual_review",
        "quality_score": data.get("quality_score"),
        "repair_passes": data.get("repair_pass", 0),
        "remaining_issues": len(data.get("quality_issues", [])),
    }
    return data


def quality_manual_review(data: dict) -> dict:
    data["result"] = {
        "status": "manual_review",
        "reason": "max_retries_exhausted",
        "repair_passes": data.get("repair_pass", 0),
        "remaining_issues": len(data.get("quality_issues", [])),
    }
    return data


OPERATIONS: dict[str, Callable[[dict], dict]] = {
    "incident-assess": incident_assess,
    "incident-prepare": incident_prepare,
    "incident-finalize": incident_finalize,
    "incident-escalate": incident_escalate,
    "quality-inspect": quality_inspect,
    "quality-repair": quality_repair,
    "quality-finalize": quality_finalize,
    "quality-manual-review": quality_manual_review,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        raise SystemExit("expected one known loop-demo operation")
    payload = json.load(sys.stdin)
    json.dump(OPERATIONS[sys.argv[1]](dict(payload)), sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
