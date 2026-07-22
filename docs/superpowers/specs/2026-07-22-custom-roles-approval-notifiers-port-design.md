# Custom Roles and Approval Notifiers Semantic Port

Port the two capabilities from `feat/custom-roles-and-approval-notifiers` into
the refactored backend without restoring monolithic `zeroth.core` ownership.

Custom role names remain strings at authentication and identity boundaries.
`zeroth.service.api.authorization.RoleRegistry` validates configured permission
names, preserves all four current built-ins (including the stricter
`platform_admin` economic permission), unions grants, and fails closed for
unknown roles. Bootstrap owns the configured registry; legacy import shims
re-export the canonical objects.

Approval notification transports live under
`zeroth.governance.approvals.notifications`. Opt-in platform settings construct
Slack and SMTP transports. Notifications contain identifiers and summary only,
never approval payloads. Request and SLA escalation notifications are fail-open;
already-escalated approvals remain idempotent. Bootstrap attaches the notifier
to the approval service. No frontend or package-version changes are included.

Verification uses focused authentication/authorization and approval tests,
followed by the full backend test and lint suites before direct integration to
`main`.
