# Run Check in CI

The root composite action needs only repository contents read access:

```yaml
permissions:
  contents: read
steps:
  - uses: actions/checkout@v4
  - uses: ./
    with:
      config: zeroth-check.yaml
      report-dir: .zeroth/check/reports
      fail-on: block,invalid
```

The CLI's canonical exits are pass `0`, canary `10`, block `20`, and invalid `30`. The GitHub
wrapper appends `check-summary.md` to `GITHUB_STEP_SUMMARY` exactly once. By default a canary is
visible but non-failing; include `canary` in `fail-on` to make exit 10 fail the job. Block and
invalid retain exits 20 and 30 after the summary is written.

Upload the report directory as a normal workflow artifact if longer retention is needed. Never
upload `.zeroth/check/recordings`.
