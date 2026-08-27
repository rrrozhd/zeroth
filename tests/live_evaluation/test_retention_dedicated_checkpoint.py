from __future__ import annotations

import importlib
from copy import deepcopy

import pytest


def test_dedicated_contract_is_exact() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")

    assert module.ACCEPTED_CRITERIA == (
        "product.retention.responsive-and-zoom",
        "product.retention.webkit-axe-and-keyboard",
        "ui.no-document-overflow",
        "ui.zoom-200-percent",
    )
    assert module.PROJECTS == (
        "desktop-1440",
        "webkit-1440",
        "desktop-1280",
        "tablet-768",
        "mobile-390",
    )


def _geometry(module, ids=None):
    return [
        {
            "id": evidence_id,
            "x": 1,
            "y": 2,
            "width": 30,
            "height": 30,
            "right": 31,
            "document_width": 390,
            "clipped_by_ancestor": None,
            "horizontally_in_document": True,
            "has_area": True,
        }
        for evidence_id in (ids or module.GEOMETRY_IDS)
    ]


def _viewport(module):
    return {
        "project": "mobile-390",
        "viewport": {"width": 390, "height": 844},
        "tenant_id": module.TENANT,
        "workspace_id": None,
        "role": module.ROLE,
        "deployment_ref": module.DEPLOYMENT,
        "deployment_version": 6,
        "graph_version_ref": module.GRAPH,
        "geometry": _geometry(module),
        "target_sizes": [
            {
                "tag": "button",
                "name": f"target-{index}",
                "width": 24,
                "height": 24,
                "meets_minimum": True,
            }
            for index in range(11)
        ],
        "document": {
            "client_width": 390,
            "scroll_width": 390,
            "reduced_motion": True,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["geometry"][0].__setitem__("clipped_by_ancestor", "shell"),
        lambda value: value["geometry"][1].__setitem__("horizontally_in_document", False),
        lambda value: value["target_sizes"][0].__setitem__("width", 23),
        lambda value: value["document"].__setitem__("scroll_width", 391),
        lambda value: value.__setitem__("deployment_version", 5),
    ],
)
def test_viewport_rejects_geometry_accessibility_or_identity_drift(mutate) -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    value = _viewport(module)
    mutate(value)

    with pytest.raises(RuntimeError):
        module._validate_viewport(value)


def test_focus_sequence_is_deterministic_and_fully_visible() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    value = [
        {
            "tag": tag,
            "aria_label": label,
            "evidence_id": evidence_id,
            "focus_visible": True,
        }
        for tag, label, evidence_id in module.FOCUS_SEQUENCE
    ]
    module._validate_focus(value)
    value[4]["focus_visible"] = False

    with pytest.raises(RuntimeError, match="focus-visible"):
        module._validate_focus(value)


def test_network_rejects_mutations_failures_and_missing_failed_response_field() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    urls = (
        "http://127.0.0.1:3000/console/retention/",
        "http://127.0.0.1:8122/health",
        "http://127.0.0.1:8122/v1/identity",
        "http://127.0.0.1:8122/v1/retention/policy",
        "http://127.0.0.1:8122/v1/retention/legal-holds",
    )
    value = {
        "requests": [{"method": "GET", "url": url, "resource_type": "fetch"} for url in urls],
        "responses": [{"url": url, "status": 200, "resource_type": "fetch"} for url in urls],
        "failed_responses": [],
    }
    module._validate_network(value)
    mutation = deepcopy(value)
    mutation["requests"][3]["method"] = "PUT"
    failure = deepcopy(value)
    failure["failed_responses"] = [{"status": 500}]

    with pytest.raises(RuntimeError, match="mutation"):
        module._validate_network(mutation)
    with pytest.raises(RuntimeError, match="failed responses"):
        module._validate_network(failure)


def test_console_requires_zero_console_page_and_unhandled_errors() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    value = {
        "events": [
            {
                "type": "log",
                "message_bytes": 2,
                "message_sha256": "a" * 64,
                "url": None,
            }
        ],
        "errors": [],
        "page_errors": [],
        "unhandled_rejections": 0,
    }
    module._validate_console(value)
    for field, bad in (
        ("errors", ["error"]),
        ("page_errors", ["page error"]),
        ("unhandled_rejections", 1),
    ):
        changed = deepcopy(value)
        changed[field] = bad
        with pytest.raises(RuntimeError, match="errors"):
            module._validate_console(changed)


def test_checkbox_restoration_requires_exact_toggle_and_restore_assertions() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    titles = (
        "Focus getByRole('checkbox', { name: 'Retention enforcement enabled' })",
        'Press "Space"',
        'Expect "toBe"',
        'Press "Space"',
        'Expect "toBe"',
    )
    snippets = (
        "const originallyChecked = await enabled.isChecked()",
        'await page.keyboard.press("Space")',
        "expect(await enabled.isChecked()).toBe(!originallyChecked)",
        'await page.keyboard.press("Space")',
        "expect(await enabled.isChecked()).toBe(originallyChecked)",
    )
    steps = [
        {
            "title": title,
            "location": {"file": module.TEST_FILE, "line": line},
            "snippet": snippet,
            "steps": [],
        }
        for title, line, snippet in zip(titles, range(282, 287), snippets, strict=True)
    ]
    module._validate_checkbox_restoration(steps)
    steps[-1]["snippet"] = "expect(true).toBe(true)"

    with pytest.raises(RuntimeError, match="restoration assertions"):
        module._validate_checkbox_restoration(steps)


def test_runtime_requires_exact_deployment_version_six() -> None:
    module = importlib.import_module("release.live_evaluation.retention_dedicated_checkpoint")
    records = {
        "health": {
            "status": "ok",
            "campaign_id": module.TENANT,
            "deployment_ref": module.DEPLOYMENT,
            "deployment_version": 6,
            "graph_version_ref": module.GRAPH,
        },
        "identity": {
            "subject": "evaluation-service",
            "tenant_id": module.TENANT,
            "workspace_id": None,
            "roles": [module.ROLE],
        },
        "retention-policy": {
            "tenant_id": module.TENANT,
            "enabled": True,
            "run_ttl_seconds": None,
            "audit_ttl_seconds": None,
        },
        "legal-holds": [
            {
                "hold_id": module.HOLD_ID,
                "tenant_id": module.TENANT,
                "run_id": module.HELD_RUN_ID,
                "reason": "preserve evidence",
                "placed_by": "operator",
                "active": True,
            }
        ],
    }
    module._validate_dedicated_runtime(records)
    records["health"]["deployment_version"] = 7

    with pytest.raises(RuntimeError, match="version 6"):
        module._validate_dedicated_runtime(records)
