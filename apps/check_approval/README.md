# Approval/resume Check reference

This offline fixture represents the resumed half of a previously persisted human approval. The
approved action is bound to one invoice and one stable tool-call ID, uses the injected durable
action repository, and exercises cancellation ambiguity during the mandatory fault matrix.

```bash
zeroth-core check run --config apps/check_approval/zeroth-check.yaml \
  --report-dir .zeroth/check/approval-report
```
