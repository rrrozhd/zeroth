# Zeroth Check quickstart

Zeroth Check records a real LangGraph tool trajectory, converts it into an explicitly reviewed
tape, replaces every live tool during replay, executes four operational faults, and emits one
explainable verdict. It is not an output-quality judge; use the existing eval subsystem for that.

Install the LangGraph adapter and inspect the offline payment reference:

```bash
pip install 'zeroth-core[langgraph]'
zeroth-core check run --config apps/check_payment/zeroth-check.yaml \
  --report-dir .zeroth/check/payment-report
zeroth-core check explain .zeroth/check/payment-report/check-verdict.json
```

To create a tape for your own target:

```bash
zeroth-core check record --config zeroth-check.yaml --case payment-7 \
  --allow-side-effects
zeroth-core check curate .zeroth/check/recordings/payment-7-payment-7-baseline.json \
  --reviewer reviewer@example.com --output checks/tapes/payment-7.json
```

Recording can execute the real model and tools. The `--allow-side-effects` flag is mandatory when
any registered tool is consequential. Raw recordings stay under the ignored `.zeroth/` tree;
only a scrubbed, reviewed TapeV1 belongs in Git.

The primary artifacts are `check-verdict.json`, `check-junit.xml`, `check-summary.md`, and
`check-terminal.txt`. Roll back a CI adoption by removing the Check workflow step; retain curated
tapes and reports for diagnosis.
