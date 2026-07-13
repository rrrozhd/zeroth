"""Drive the vendor-dd scenarios end-to-end over HTTP.

Requires both services running (see README):

    uv run python -m apps.vendor_dd.seed          # once
    uv run python -m apps.vendor_dd.entrypoint    # terminal 1 (port 8730)
    PORT=8731 ZEROTH_DEPLOYMENT_REF=vendor-dd-chat \\
        uv run python -m apps.vendor_dd.entrypoint  # terminal 2

Then:

    uv run python -m apps.vendor_dd.runbook

Scenarios: clean vendor (auto lane + webhook), sanctioned vendor (approval
lane), budget-cap trip, and a two-turn follow-up chat on one thread. Run ids
are written to ``.runbook_state.json`` for ``verify.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / ".runbook_state.json"

MAIN_URL = "http://127.0.0.1:8730"
CHAT_URL = "http://127.0.0.1:8731"
API_KEY = "vendor-dd-ops-key"
TENANT = "tenant-acme"
RECEIVER_PORT = 8790

from apps.vendor_dd.fixtures.dossiers import CLEAN_VENDOR, RISKY_VENDOR

# ---------------------------------------------------------------------------
# Tiny HTTP helpers (stdlib-only so the runbook needs no extra deps).
# ---------------------------------------------------------------------------


def call(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, method=method)
    request.add_header("X-API-Key", API_KEY)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urlopen(request, timeout=15) as resp:  # noqa: S310 - local service
        return json.loads(resp.read() or b"{}")


def submit_run(base_url: str, input_payload: dict, thread_id: str | None = None) -> str:
    payload: dict = {"input_payload": input_payload}
    if thread_id:
        payload["thread_id"] = thread_id
    return call("POST", f"{base_url}/v1/runs", payload)["run_id"]


def wait_run(base_url: str, run_id: str, *, until: tuple[str, ...], timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = call("GET", f"{base_url}/v1/runs/{run_id}")
        if run["status"] in until:
            return run
        time.sleep(1.5)
    raise TimeoutError(f"run {run_id} did not reach {until} within {timeout}s")


def require_service(base_url: str, name: str) -> None:
    try:
        health = call("GET", f"{base_url}/health")
    except (URLError, OSError) as exc:
        print(f"FAIL: {name} service is not reachable at {base_url} ({exc}).")
        print("Start it first — see the runbook docstring or README.")
        raise SystemExit(1) from exc
    print(f"{name}: {health['status']} (deployment {health['deployment_ref']})")


# ---------------------------------------------------------------------------
# Webhook receiver: records deliveries and verifies the HMAC signature.
# ---------------------------------------------------------------------------


class _Receiver(BaseHTTPRequestHandler):
    deliveries: list[dict] = []
    secret: str = ""

    def do_POST(self):  # noqa: N802 - stdlib naming
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        signature = self.headers.get("X-Zeroth-Signature", "")
        expected = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        verified = bool(self.secret) and hmac.compare_digest(
            signature.removeprefix("sha256="), expected
        )
        type(self).deliveries.append(
            {"payload": json.loads(raw or b"{}"), "signature_verified": verified}
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence default logging
        return


def start_receiver() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", RECEIVER_PORT), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def econ_headers() -> dict:
    from zeroth.core.econ.service_auth import mint_econ_service_token

    token = mint_econ_service_token()
    if token is None:
        raise SystemExit("could not mint econ_plane admin token (regulus extra missing?)")
    return {"Authorization": f"Bearer {token}"}


def set_budget_cap(cap_usd: float) -> None:
    call(
        "PUT",
        f"{MAIN_URL}/regulus/v1/budget/tenants/{TENANT}",
        {"budget_cap_usd": cap_usd},
        headers=econ_headers(),
    )


def main() -> int:
    print("── vendor-dd runbook ─────────────────────────────────────")
    require_service(MAIN_URL, "main")
    require_service(CHAT_URL, "chat")
    state: dict = {"tenant": TENANT}

    # Webhook subscription -> local receiver.
    start_receiver()
    subscription = call(
        "POST",
        f"{MAIN_URL}/v1/webhooks/subscriptions",
        {
            "deployment_ref": "vendor-dd",
            "tenant_id": TENANT,
            "target_url": f"http://127.0.0.1:{RECEIVER_PORT}/hook",
            "event_types": ["run.completed", "approval.requested"],
        },
    )
    _Receiver.secret = subscription["secret"]
    print(f"webhook subscription {subscription['subscription_id']} -> :{RECEIVER_PORT}/hook")

    # Make sure no previous cap interferes.
    set_budget_cap(1000.0)
    time.sleep(3)  # let the budget cache expire

    # Scenario 1 — clean vendor: auto lane, report delivered by webhook.
    print("\n[1] clean vendor (auto lane)")
    run_id = submit_run(MAIN_URL, CLEAN_VENDOR)
    run = wait_run(MAIN_URL, run_id, until=("succeeded", "failed", "dead_letter"))
    report = (run.get("terminal_output") or {}).get("report", {})
    print(
        f"    status={run['status']} tier={report.get('tier')} "
        f"score={report.get('risk_score')} decision={report.get('decision')}"
    )
    assert run["status"] == "succeeded", run.get("failure_state")
    assert report.get("tier") in ("low", "medium"), report
    state["clean_run_id"] = run_id

    # Scenario 2 — sanctioned vendor: pauses for human review, then completes.
    print("\n[2] sanctioned vendor (approval lane)")
    run_id = submit_run(MAIN_URL, RISKY_VENDOR)
    run = wait_run(MAIN_URL, run_id, until=("paused_for_approval", "failed"))
    assert run["status"] == "paused_for_approval", run.get("failure_state")
    approval_id = run["approval_paused_state"]["approval_id"]
    drivers = run["approval_paused_state"]["input_payload"].get("drivers", [])
    print(f"    paused at risk-review; approval={approval_id}")
    print(f"    drivers: {'; '.join(drivers)}")
    call(
        "POST",
        f"{MAIN_URL}/v1/deployments/vendor-dd/approvals/{approval_id}/resolve",
        {"decision": "approve"},
    )
    run = wait_run(MAIN_URL, run_id, until=("succeeded", "failed"))
    report = (run.get("terminal_output") or {}).get("report", {})
    print(f"    status={run['status']} tier={report.get('tier')} decision={report.get('decision')}")
    assert run["status"] == "succeeded", run.get("failure_state")
    assert report.get("tier") in ("high", "critical"), report
    state["risky_run_id"] = run_id
    state["approval_id"] = approval_id

    # Scenario 3 — budget cap trips before any LLM call.
    print("\n[3] budget cap trip")
    set_budget_cap(0.0)
    time.sleep(3)
    run_id = submit_run(MAIN_URL, CLEAN_VENDOR)
    run = wait_run(MAIN_URL, run_id, until=("failed", "succeeded"))
    message = (run.get("failure_state") or {}).get("message", "")
    print(f"    status={run['status']}: {message}")
    assert run["status"] == "failed" and "budget" in message.lower(), run
    state["budget_run_id"] = run_id
    set_budget_cap(1000.0)
    time.sleep(3)

    # Scenario 4 — follow-up chat: two runs, one thread, real recall.
    print("\n[4] follow-up chat (persistent thread)")
    thread_id = f"thread-{int(time.time())}"
    first_question = "Why was Crimson Bridge Analytics rejected?"
    run_id = submit_run(
        CHAT_URL,
        {"messages": [{"role": "human", "content": first_question}], "vendor": "crimson"},
        thread_id=thread_id,
    )
    run = wait_run(CHAT_URL, run_id, until=("succeeded", "failed"))
    print(f"    turn 1: {(run.get('terminal_output') or {}).get('reply', '')[:90]}")
    state["chat_run_1"] = run_id
    run_id = submit_run(
        CHAT_URL,
        {
            "messages": [{"role": "human", "content": "What was my first question?"}],
            "vendor": "crimson",
        },
        thread_id=thread_id,
    )
    run = wait_run(CHAT_URL, run_id, until=("succeeded", "failed"))
    reply = (run.get("terminal_output") or {}).get("reply", "")
    print(f"    turn 2: {reply[:110]}")
    assert first_question in reply, "thread did not recall the first turn"
    state["chat_run_2"] = run_id
    state["chat_thread_id"] = thread_id

    # Webhook deliveries should have arrived by now (delivery worker polls).
    print("\n[5] webhook deliveries")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and len(_Receiver.deliveries) < 2:
        time.sleep(2)
    events = [d["payload"].get("event_type") for d in _Receiver.deliveries]
    verified = [d["signature_verified"] for d in _Receiver.deliveries]
    print(f"    received {len(_Receiver.deliveries)} deliveries: {events}")
    print(f"    HMAC signatures verified: {verified}")
    assert _Receiver.deliveries, "no webhook deliveries received"
    assert all(verified), "webhook HMAC signature verification failed"
    state["webhook_events"] = events

    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\nOK — all scenarios passed. State written to {STATE_FILE.name}.")
    print("Next: uv run python -m apps.vendor_dd.verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
