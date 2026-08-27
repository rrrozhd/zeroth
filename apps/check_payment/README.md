# Payment Check reference

This offline fixture demonstrates a consequential payment tool, a curated synthetic tape, three
tape-only replays, and all four mandatory faults. The live implementation writes only to a local
ledger and is reachable solely during an explicitly consented `check record` command.

```bash
zeroth-core check run --config apps/check_payment/zeroth-check.yaml \
  --report-dir .zeroth/check/payment-report
```
