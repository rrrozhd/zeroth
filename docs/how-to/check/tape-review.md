# Review and approve a tape

Never copy a raw recording into `checks/tapes`. `check curate` verifies the raw source digest,
normalizes JSON, scans and deterministically replaces suspected secrets, rescans, recomputes any
argument fingerprint and ActionIdentity changed by scrubbing, then writes atomically and reloads
the result.

Before approval, verify:

- `schema_version`, `normalization_version`, and `action_identity_version` are exact V1 values;
- each tool has `read_only` or `side_effecting`, an originating `tool_call_id`, arguments, result
  availability, and the expected result/error shape;
- model observations contain no provider bodies or message content;
- the case input and invocation config contain no credentials or unnecessary personal data;
- tool name, schema digest, side-effect class, and logical scenario still describe the intended
  behavior.

TapeV1 approval fields are `raw_source_digest`, `scrubber_version`, `secret_rules_version`,
`reviewer_id`, `approved_at`, `identity_changed_by_scrubbing`, and
`curated_content_digest`. A heuristic scanner reduces risk but cannot prove absence of secrets;
human review is part of the security boundary.
