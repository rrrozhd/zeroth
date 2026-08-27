# Troubleshoot Check verdicts

Start with `zeroth-core check explain REPORT/check-verdict.json`; it reads the verdict only and
never reruns the target.

| Reason | Meaning / first check |
|---|---|
| `config_invalid` | Validate exact V1 constants and unknown YAML keys. |
| `tape_unapproved` | Select a curated TapeV1, not a raw recording. |
| `tape_schema_invalid` | Inspect schema version and content/source digests. |
| `no_side_effecting_occurrence` | Full Check requires one curated consequential call. |
| `target_rebuild_failed` | Verify `module:build_target`, stable input, and repository seam use. |
| `fault_not_observed` | Inspect fault events for missing injection/recovery points. |
| `infrastructure_failed` | Inspect the exact worker's state directory and process error type. |
| `duplicate_effect` | Inspect markers and the shared action repository record. |
| `live_tool_attempted` | Find an unregistered/dynamic tool escape path. |
| `action_identity_mismatch` | Compare case/run/tool/schema/call-ID/argument fingerprints. |
| `unsafe_retry` | Confirm AMBIGUOUS/COMPLETED actions are fenced. |
| `cancellation_swallowed` | Preserve `CancelledError` through the governed boundary. |
| `restart_reexecuted` | Confirm restart workers share receipt and checkpoint paths. |
| `replay_mismatch_safety` | Inspect the reason-coded fail-closed matcher fact. |
| `ordinary_quorum_missed` | Compare normalized trajectories; semantic prose is intentionally absent. |
| `usage_incomplete` | Supply provider/model, all counts, and input/output detail maps. |
| `optional_fault_inconclusive` | Inspect the add-on's injection and recovery events. |

Do not “fix” an AMBIGUOUS action by deleting its row. Reconcile it against the downstream system
with an operator reference, then retain that evidence.
