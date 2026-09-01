# Resolve launch policy inputs

This is an operational drafting checklist, not legal advice or a publishable
policy. Resolve every bracketed value with the product owner and qualified
counsel before accepting a production payment. Do not silently infer the legal
entity, governing law, consumer rights, retention period, or refund promise from
the codebase.

## Owner and contact facts

```text
Legal entity name: [LEGAL_ENTITY_NAME]
Entity address and country/state: [REGISTERED_ADDRESS]
Governing law and dispute forum: [COUNSEL_APPROVED_JURISDICTION]
Privacy/data-rights email: [PRIVACY_EMAIL]
Support email and response target: [SUPPORT_EMAIL / RESPONSE_TARGET]
Security/incident email: [SECURITY_EMAIL]
Minimum customer age and permitted customer regions: [AGE / REGIONS]
Policy effective date and version owner: [DATE / OWNER]
```

## Product and billing facts

- Describe Solo exactly as sold: $39/month after a 14-day trial, with the
  enforced limits in [hosted identity and commerce](hosted-commerce.md).
- State when the trial converts, how renewal works, and how cancellation affects
  current-period access. Match Paddle configuration and webhook behavior.
- Decide any supplier-provided refund promise: `[REFUND_PROMISE]`. Paddle is the
  seller/merchant of record, processes eligible refunds, and may apply mandatory
  buyer rights. Zeroth must not promise less than applicable law or claim it
  directly refunds a Paddle buyer.
- Publish the customer-portal and Paddle buyer-support paths, the support
  address, acceptable use, suspension grounds, and what happens to retained
  evidence after cancellation.

Paddle's official [Master Services Agreement](https://www.paddle.com/legal/terms)
says the supplier must route agreed refunds through Paddle rather than pay the
buyer directly. Paddle's [Refund Policy](https://www.paddle.com/legal/refund-policy)
also makes mandatory local rights controlling. Confirm the current versions and
the configured seller information immediately before publication.

## Data map and privacy facts

Complete the map from actual production configuration, not intended design:

| Data category | Purpose | System/processors | Retention/deletion | Customer control |
| --- | --- | --- | --- | --- |
| Account identity and organization membership | Authentication and tenant scope | Zeroth, WorkOS | `[PERIOD]` | `[ACCESS/DELETE]` |
| Billing and subscription identifiers | Entitlement and support | Zeroth, Paddle | `[PERIOD]` | `[PORTAL/REQUEST]` |
| Economic events and outcome evidence | Debugging and backtests | Zeroth, Railway, configured model providers | `[PERIOD]` | `[EXPORT/DELETE]` |
| Operational logs, IP/device data | Security and reliability | `[SYSTEMS]` | `[PERIOD]` | `[REQUEST]` |
| Support communications | Customer support | `[SYSTEMS]` | `[PERIOD]` | `[REQUEST]` |

Document whether prompts, outputs, traces, subject identifiers, or sensitive
personal data are prohibited, optional, or processed. Document model-provider
data flows separately: WorkOS, Paddle, and Railway are not the only processors
if a hosted backtest calls a provider.

List the deployed subprocessors, processing locations, transfer mechanism, DPA
availability, deletion workflow, backup-retention behavior, incident process,
and data-rights response process. Official starting points are WorkOS's
[DPA](https://workos.com/legal/data-processing-addendum) and
[subprocessor policies](https://workos.com/legal/policies), plus Railway's
[DPA](https://railway.com/legal/dpa). Vendor terms do not replace Zeroth's own
controller/processor analysis or customer notice.

## Minimum public pages and acceptance

Before the first production transaction, publish stable URLs for:

- Terms of Service and Acceptable Use;
- Privacy, cookies if applicable, data deletion, and subprocessor disclosure;
- cancellation and refund information consistent with Paddle;
- support scope, contact, and response expectation; and
- security/incident contact.

Acceptance requires owner/counsel approval, a broken-link check from signup and
checkout, a test email to every contact, and archived copies of the effective
versions. Re-review the pages whenever data collection, providers, price,
retention, supported regions, or merchant-of-record configuration changes.
