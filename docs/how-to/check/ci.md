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

The `Zeroth Check consumer fixture` job in the repository's existing CI workflow continuously
exercises this exact composite action against `apps/check_payment` with read-only permissions.
Treat that job as the release gate before exposing a new Action revision to consumers.

Marketplace publication is not required for a pilot. An external consumer can pin a reviewed
revision directly using `uses: OWNER/zeroth@FULL_COMMIT_SHA`. Prefer the full immutable commit over
a moving branch or major-version tag until the pilot has established compatibility and support
expectations.
