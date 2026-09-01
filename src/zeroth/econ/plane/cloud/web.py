"""Minimal same-origin customer surface for self-serve Zeroth Cloud."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.authkit import require_authkit_enabled
from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate
from zeroth.econ.plane.cloud.keys_service import issue_api_key, list_api_keys, revoke_api_key
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
from zeroth.econ.plane.cloud.paddle import (
    PaddleGateway,
    get_paddle_gateway,
    require_paddle_enabled,
)
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["zeroth-cloud-self-service"])

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _page(title: str, content: str) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="referrer" content="no-referrer">
  <title>{escape(title)} · Zeroth</title>
  <style>
    :root {{ color-scheme: light; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-width: 960px; color: #171717; background: #f4f1e8; }}
    main {{ width: 880px; margin: 0 auto; padding: 72px 0 96px; }}
    header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #171717;
      padding-bottom: 18px; margin-bottom: 56px; }}
    h1 {{ font: 600 42px/1.08 ui-sans-serif, system-ui, sans-serif; max-width: 760px; margin: 0 0 20px; }}
    h2 {{ font: 600 21px/1.2 ui-sans-serif, system-ui, sans-serif; margin: 0 0 16px; }}
    p, li {{ font: 16px/1.65 ui-sans-serif, system-ui, sans-serif; }}
    .muted {{ color: #5b5b55; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin: 32px 0; }}
    .panel {{ border: 1px solid #171717; background: #fffdf6; padding: 24px; }}
    .key {{ display: block; overflow-wrap: anywhere; border: 2px solid #171717;
      background: #fff; padding: 18px; margin: 18px 0; user-select: all; }}
    .actions {{ display: flex; gap: 12px; align-items: center; margin-top: 24px; }}
    button, .button {{ appearance: none; border: 1px solid #171717; border-radius: 0;
      background: #171717; color: #fff; cursor: pointer; display: inline-block;
      font: 600 14px/1 ui-sans-serif, system-ui, sans-serif; padding: 14px 18px;
      text-decoration: none; }}
    button.secondary, .button.secondary {{ background: transparent; color: #171717; }}
    table {{ border-collapse: collapse; width: 100%; background: #fffdf6; margin: 18px 0 34px; }}
    th, td {{ border: 1px solid #171717; padding: 12px; text-align: left; }}
    code {{ font-family: inherit; }}
    form {{ margin: 0; }}
  </style>
</head>
<body><main><header><strong>ZEROTH / ECONOMIC DEBUGGER</strong><span>SOLO</span></header>{content}</main></body>
</html>"""
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": _CSP,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


def activation_page(*, tenant_id: str, key_id: str, api_key: str | None) -> HTMLResponse:
    """Render the browser-only activation result without retaining the secret."""
    if api_key is None:
        reveal = """
<div class="panel"><h2>The original key was already revealed</h2>
<p>Create a replacement from your account if you did not save it.</p></div>"""
    else:
        reveal = f"""
<div class="panel"><h2>Copy this key now</h2>
<p class="muted">It is stored only as a hash and cannot be shown again.</p>
<code class="key">{escape(api_key)}</code></div>"""
    return _page(
        "Trial ready",
        f"""<h1>Your Zeroth trial is ready.</h1>
<p>Run one bounded production-economic backtest, then keep continuous evidence for
$39/month. Paddle will show the 14-day trial and renewal terms before confirmation.</p>
{reveal}
<div class="grid">
  <div class="panel"><h2>Tenant</h2><code>{escape(tenant_id)}</code></div>
  <div class="panel"><h2>Key record</h2><code>{escape(key_id)}</code></div>
</div>
<div class="actions">
  <form action="/account/checkout" method="post"><button type="submit">Continue to Paddle</button></form>
  <a class="button secondary" href="/account">Open account</a>
</div>""",
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing(_enabled: None = Depends(require_authkit_enabled)) -> HTMLResponse:  # noqa: B008
    return _page(
        "Economic debugger",
        """<h1>Economic debugger for production AI.</h1>
<p>See which model or workflow change improves outcomes before it reaches production.
Retain the decision, the measured cost, and the evidence behind it.</p>
<div class="grid">
  <div class="panel"><h2>14-day trial</h2><p>One bounded backtest and 100 provider calls.</p></div>
  <div class="panel"><h2>$39/month</h2><p>Three backtests, 300 provider calls, retained history and scheduled decisions.</p></div>
</div>
<div class="actions"><a class="button" href="/v1/cloud/auth/login">Start with AuthKit</a></div>""",
    )


def _subscription(db: ScopedSession, tenant_id: str) -> CloudSubscription:
    subscription = db.get(CloudSubscription, tenant_id)
    if subscription is None:
        raise HTTPException(status_code=409, detail="Cloud subscription is not initialized")
    return subscription


@router.get("/account", response_class=HTMLResponse, include_in_schema=False)
def account(
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
) -> HTMLResponse:
    subscription = _subscription(db, user.tenant_id)
    keys = list_api_keys(db)
    usage = {
        row.meter: row.quantity
        for row in db.scalars(
            select(CloudUsageCounter).where(
                CloudUsageCounter.period_start == subscription.period_start
            )
        )
    }
    key_rows = "".join(
        f"<tr><td>{escape(key.name)}</td><td>••••{escape(key.last_four)}</td>"
        f"<td>{'revoked' if key.revoked_at else 'active'}</td>"
        f"<td>{'' if key.revoked_at else _revoke_form(key.key_id)}</td></tr>"
        for key in keys
    ) or '<tr><td colspan="4">No keys</td></tr>'
    portal = (
        '<form action="/account/portal" method="post"><button class="secondary" '
        'type="submit">Manage billing</button></form>'
        if subscription.external_customer_id
        else ""
    )
    checkout = (
        '<form action="/account/checkout" method="post"><button type="submit">'
        'Continue to Paddle</button></form>'
        if not subscription.external_customer_id
        else ""
    )
    return _page(
        "Account",
        f"""<h1>Account economics.</h1>
<div class="grid">
  <div class="panel"><h2>{escape(subscription.plan.title())}</h2>
    <p>Status: {escape(subscription.status)}</p>
    <p class="muted">Period ends {escape(subscription.period_end.isoformat())}</p></div>
  <div class="panel"><h2>Usage</h2>
    <p>Backtests: {usage.get('backtests', 0)} · provider calls: {usage.get('backtest_calls', 0)}</p>
    <p>Events: {usage.get('events', 0)} · decisions: {usage.get('decision_scans', 0)}</p></div>
</div>
<h2>Project keys</h2>
<table><thead><tr><th>Name</th><th>Fingerprint</th><th>Status</th><th></th></tr></thead>
<tbody>{key_rows}</tbody></table>
<div class="actions">
  <form action="/account/api-keys" method="post"><button class="secondary" type="submit">Create replacement key</button></form>
  {checkout}{portal}
</div>""",
    )


def _revoke_form(key_id: str) -> str:
    return (
        f'<form action="/account/api-keys/{escape(key_id)}/revoke" method="post">'
        '<button class="secondary" type="submit">Revoke</button></form>'
    )


@router.post("/account/checkout", include_in_schema=False)
def browser_checkout(
    _enabled: None = Depends(require_paddle_enabled),  # noqa: B008
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    gateway: PaddleGateway = Depends(get_paddle_gateway),  # noqa: B008
) -> RedirectResponse:
    price_id = settings.paddle_solo_price_id
    if not price_id:
        raise HTTPException(status_code=503, detail="Billing price is not configured")
    url = gateway.create_checkout(price_id=price_id, tenant_id=user.tenant_id)
    return RedirectResponse(url, status_code=303)


@router.post("/account/portal", include_in_schema=False)
def browser_portal(
    _enabled: None = Depends(require_paddle_enabled),  # noqa: B008
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    gateway: PaddleGateway = Depends(get_paddle_gateway),  # noqa: B008
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
) -> RedirectResponse:
    subscription = _subscription(db, user.tenant_id)
    if not subscription.external_customer_id or subscription.billing_provider != "paddle":
        raise HTTPException(status_code=409, detail="No Paddle customer exists for this tenant")
    url = gateway.create_portal(
        customer_id=subscription.external_customer_id,
        subscription_id=subscription.external_subscription_id,
    )
    return RedirectResponse(url, status_code=303)


@router.post("/account/api-keys", response_class=HTMLResponse, include_in_schema=False)
def browser_create_key(
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
) -> HTMLResponse:
    name = f"browser-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    reveal = issue_api_key(
        db,
        ApiKeyCreate(name=name, roles=["Analyst"]),
        subject=user.sub,
        workspace_id=user.workspace_id,
    )
    return _page(
        "Key created",
        f"""<h1>Copy this key now.</h1>
<p class="muted">Zeroth stores only its hash. Losing it requires another replacement.</p>
<code class="key">{escape(reveal.api_key)}</code>
<div class="actions"><a class="button secondary" href="/account">Return to account</a></div>""",
    )


@router.post("/account/api-keys/{key_id}/revoke", include_in_schema=False)
def browser_revoke_key(
    key_id: str,
    _user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
) -> RedirectResponse:
    if not revoke_api_key(db, key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return RedirectResponse("/account", status_code=303)


__all__ = ["activation_page", "router"]
