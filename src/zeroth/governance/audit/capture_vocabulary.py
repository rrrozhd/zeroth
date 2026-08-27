"""What each allowlisted metadata key may hold, and the closed sets it holds it from.

``execution_metadata`` and approval metadata are free-form ``dict[str, Any]``
that producers fill with whatever they were holding -- a prompt under
``execution_metadata["prompt"]``, a password nested in a tuple, a raw exception
string. Best-effort key-based redaction cannot close that: it only masks the key
names someone thought of. An allowlist of key *names* did not close it either,
because every allowlisted key accepted any short string: ``correlation_id`` is
filled from a client-supplied ``X-Correlation-ID`` header, so a credential pasted
into that header was persisted verbatim. Each allowlisted key therefore declares
a :class:`MetadataKind`, and a value that does not fit its key's kind is
summarized rather than retained.

**Why identifiers are their own kind rather than OPAQUE or VOCABULARY.**
``branch_id`` and ``join_key`` are not producer payload: the runtime mints
``branch_id`` from the record's own ``run_id`` and branch index, and ``join_key``
is the business-request id a retention sweep must match against econ events.
OPAQUE hashes what it retains, which satisfies a key-presence check while
breaking every consumer that *compares* the value -- the branch-fan-out audits
and :class:`~zeroth.governance.retention.erasure_service.RetentionErasureService`
both compare. A vocabulary cannot enumerate them, because they are minted per
run. :data:`MetadataKind.IDENTIFIER` therefore validates the shape an identifier
has and keeps the value itself. It is the one kind that still trades on shape,
and it is deliberately the narrowest place that trade is made.

A metadata key used to be allowed to keep its value verbatim when the value
*looked* structural: bounded, lower-case, punctuated only the way a vocabulary
term is punctuated. That is a shape check, and a shape check is not a provenance
check. ``ghp_abcdefghijklmnopqrstuvwxyz`` is bounded, lower-case and punctuated
exactly like ``runtime_not_allowed``, so a credential filed under ``reason_code``
-- or ``status``, or any other label key -- was persisted verbatim under the
default policy. No regex can separate the two, because the only difference
between them is where the string came from.

**So the projection stops asking what a value looks like and asks whether this
codebase can mint it.** Every key that keeps text keeps it only when the text is
a member of that key's closed vocabulary here; anything else is replaced by the
digest/schema/count summary. The vocabulary *is* the provenance check: a term
gets into one of these sets by somebody editing this module, which is the same
review gate that guards the allowlist itself.

**Why a registry for reason codes rather than hashing them.** Reason codes are
the one open-ended space here -- they are authored across many producers, and
:func:`normalize_reason_code` mints one from any exception class name. Hashing
them would have been the smaller change and is what an "unknown provenance"
rule implies, but R4 requires the *decision* metadata to survive a
metadata-only capture, and a denial whose reason is a hex digest tells an
auditor that something was refused without telling them why. So reason codes
get the registry: :data:`REASON_CODES` enumerates the failure and denial names
this codebase can actually produce, and a code outside it is summarized -- the
digest still correlates two records that failed the same way, and adding a new
producer-side code is a deliberate edit here. The registry is fail-closed by
construction: forgetting to register a code loses readability, never
confidentiality.

Keys whose value space is genuinely open *and* whose text carries no decision
(``model_name``, ``operation``, ``policy_version``, ``provider``, ``status``)
are not listed here at all. They are hashed instead -- see
:data:`~zeroth.governance.audit.capture_projection.METADATA_KINDS`.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Mapping
from enum import StrEnum


class MetadataKind(StrEnum):
    """What one allowlisted metadata key is allowed to hold.

    The kind is the whole of the per-key contract: a value that does not match
    its key's kind is replaced by a summary, so widening what a key accepts
    takes a deliberate edit here rather than a producer writing something new
    into it.
    """

    NUMBER = "number"
    BOOLEAN = "boolean"
    DIGEST = "digest"
    VOCABULARY = "vocabulary"
    IDENTIFIER = "identifier"
    OPAQUE = "opaque"


# The metadata keys the capture boundary recognises as structural, each with the
# kind of value it may carry. A key outside this mapping is not retained under a
# metadata-only capture, whatever it holds.
#
# OPAQUE marks the values whose text is chosen outside this codebase or carries
# no verdict -- ``correlation_id`` comes straight from a client request header,
# ``assistant_id`` from a client-supplied path, ``reviewer`` from whoever
# submitted an approval, ``operation`` partly from a client-supplied protocol
# method, ``model_name`` and ``provider`` from deployment configuration, and
# ``status`` duplicates the typed ``NodeAuditRecord.status`` column. None of them
# is ever retained verbatim; each is replaced by a stable digest, which still
# correlates two records that shared one while carrying none of the text.
METADATA_KINDS: Mapping[str, MetadataKind] = {
    "active_count": MetadataKind.NUMBER,
    "admission_digest": MetadataKind.DIGEST,
    # ZER-26 side-effect operations. Flat, not nested: the projection retains
    # only allowlisted top-level keys, so a nested block is dropped before it is
    # ever persisted. ``operation_key`` is IDENTIFIER because consumers must be
    # able to *compare* two records that describe one logical operation -- an
    # OPAQUE digest would satisfy a presence check while breaking correlation.
    # ``operation_target_ref`` stays OPAQUE: it can carry a manifest ref chosen
    # outside this codebase.
    "operation_first_execution": MetadataKind.BOOLEAN,
    "operation_key": MetadataKind.IDENTIFIER,
    "operation_reconciliation_exhausted": MetadataKind.BOOLEAN,
    "operation_reconciliation_required": MetadataKind.BOOLEAN,
    "operation_replay_suppressed": MetadataKind.BOOLEAN,
    "operation_residual_duplicate_risk": MetadataKind.BOOLEAN,
    "operation_state": MetadataKind.VOCABULARY,
    "operation_support": MetadataKind.VOCABULARY,
    "operation_target_ref": MetadataKind.OPAQUE,
    "resolution_reason_sha256": MetadataKind.DIGEST,
    "receipt_sha256": MetadataKind.DIGEST,
    "admitted": MetadataKind.BOOLEAN,
    "assistant_id": MetadataKind.OPAQUE,
    "attempt": MetadataKind.NUMBER,
    "branch_id": MetadataKind.IDENTIFIER,
    "branch_index": MetadataKind.NUMBER,
    # Approval-bearing subgraphs retain the exact runtime-minted ancestry IDs
    # needed to prove which child was resumed and which parent worker was
    # notified.  Deployment refs are operator-authored, so only their digest is
    # retained here.
    "child_run_id": MetadataKind.IDENTIFIER,
    "direct_child_run_id": MetadataKind.IDENTIFIER,
    "continuation_parent_run_id": MetadataKind.IDENTIFIER,
    "child_deployment_ref_sha256": MetadataKind.DIGEST,
    # ZER-37 repository-unit provenance. The ids are IDENTIFIER for the same
    # reason operation_key is: an auditor must be able to *correlate* every run
    # of one installation/repository/checkout, and the values are platform- or
    # GitHub-minted numerics and ids, not author text -- producers write them
    # as strings ("8891"). The digests pin what ran: ``repo_commit_sha`` is
    # retained as the bare 40-hex string StagedCheckout carries (the DIGEST
    # shape admits unprefixed hex); ``repo_config_digest`` and
    # ``repo_tree_digest`` are ``sha256:``-prefixed, and
    # ``repo_manifest_digest`` is the bare 64-hex compute_manifest_digest
    # returns.
    "checkout_id": MetadataKind.IDENTIFIER,
    "repo_commit_sha": MetadataKind.DIGEST,
    "repo_config_digest": MetadataKind.DIGEST,
    "repo_installation_id": MetadataKind.IDENTIFIER,
    "repo_manifest_digest": MetadataKind.DIGEST,
    "repo_repository_id": MetadataKind.IDENTIFIER,
    "repo_tree_digest": MetadataKind.DIGEST,
    "smoke_passed": MetadataKind.BOOLEAN,
    "budget_cap_usd": MetadataKind.NUMBER,
    "budget_check_degraded": MetadataKind.BOOLEAN,
    "budget_spend_usd": MetadataKind.NUMBER,
    "compatibility_fingerprint": MetadataKind.DIGEST,
    "correlation_id": MetadataKind.OPAQUE,
    "cost_event_id": MetadataKind.OPAQUE,
    "cost_usd": MetadataKind.NUMBER,
    "decision": MetadataKind.VOCABULARY,
    "disposition": MetadataKind.VOCABULARY,
    "duration_ms": MetadataKind.NUMBER,
    "enforcement_applied": MetadataKind.BOOLEAN,
    "effective_limit": MetadataKind.NUMBER,
    "governance_level": MetadataKind.VOCABULARY,
    "input_sha256": MetadataKind.DIGEST,
    "input_size_bytes": MetadataKind.NUMBER,
    "join_key": MetadataKind.IDENTIFIER,
    # Exact manifest refs may name operator-authored or external resources. The
    # stable digest supports run-to-manifest correlation without retaining that text.
    "manifest_ref_sha256": MetadataKind.DIGEST,
    # ZER-26/AUD-008 worker-level lease events: which worker was displaced and
    # at which generation. The worker id is IDENTIFIER for the same reason
    # operation_key is: consumers must correlate the displaced worker across
    # records, and it is machine-minted uuid4 hex, so retaining it leaks nothing.
    "lease_generation": MetadataKind.NUMBER,
    "model_name": MetadataKind.OPAQUE,
    "network_mode": MetadataKind.VOCABULARY,
    "node_kind": MetadataKind.VOCABULARY,
    "operation": MetadataKind.OPAQUE,
    "output_sha256": MetadataKind.DIGEST,
    "output_size_bytes": MetadataKind.NUMBER,
    "policy_version": MetadataKind.OPAQUE,
    "provider": MetadataKind.OPAQUE,
    "reason_code": MetadataKind.VOCABULARY,
    # A retrieval cardinality is structural evidence, not retrieved content.
    # Keeping the number makes an exact miss observable without retaining the
    # query, sources, or chunks that produced it.
    "retrieval_result_count": MetadataKind.NUMBER,
    "retry_count": MetadataKind.NUMBER,
    "rendered_prompt_sha256": MetadataKind.DIGEST,
    "reviewer": MetadataKind.OPAQUE,
    "sandbox_strictness_mode": MetadataKind.VOCABULARY,
    "status": MetadataKind.OPAQUE,
    "upstream_status_code": MetadataKind.NUMBER,
    "utilization": MetadataKind.NUMBER,
    "worker_id": MetadataKind.IDENTIFIER,
    # Webhook operator and delivery transitions. The IDs are generated by the
    # service, the destination is retained only as a digest, and readable text
    # is restricted to the closed event/transition vocabularies below.
    "webhook_subscription_id": MetadataKind.IDENTIFIER,
    "webhook_delivery_id": MetadataKind.IDENTIFIER,
    "webhook_event_id": MetadataKind.IDENTIFIER,
    "webhook_dead_letter_id": MetadataKind.IDENTIFIER,
    "webhook_event_type": MetadataKind.VOCABULARY,
    "webhook_transition": MetadataKind.VOCABULARY,
    "target_url_sha256": MetadataKind.DIGEST,
    # Durable thread-state facts. These are runtime-minted booleans and expose
    # no message content; keeping them makes compaction persistence observable
    # under the default metadata-only capture posture.
    "thread_state_checkpointed": MetadataKind.BOOLEAN,
    "compacted_thread_state_saved": MetadataKind.BOOLEAN,
    "context_compaction_applied": MetadataKind.BOOLEAN,
    "context_compaction_strategy": MetadataKind.VOCABULARY,
    "context_tokens_before": MetadataKind.NUMBER,
    "context_tokens_after": MetadataKind.NUMBER,
    "context_messages_before": MetadataKind.NUMBER,
    "context_messages_after": MetadataKind.NUMBER,
    # Prompt-template control-plane mutations. Operator-authored names are
    # retained only as a digest; versions and closed transitions stay readable.
    "template_name_sha256": MetadataKind.DIGEST,
    "template_version": MetadataKind.NUMBER,
    "template_transition": MetadataKind.VOCABULARY,
}

# Derived, never maintained alongside: the two cannot drift apart.
ALLOWED_METADATA_KEYS = frozenset(METADATA_KINDS)

# A reason code is a short name, not a sentence; the bound is what stops an
# interpolated message from being normalized into a "code".
_MAX_REASON_CODE_CHARS = 64


def normalize_reason_code(name: object) -> str | None:
    """Render a producer's failure or denial *name* as a stable label-shaped code.

    Call this on a *name* -- an exception class, a denial branch -- never on a
    message. Lower-cased ASCII words survive the transform, so a sentence that
    interpolated a value would carry that value through it. The projection is
    the backstop: a normalized code that is not in :data:`REASON_CODES` is
    summarized rather than retained, so a smuggled value cannot reach storage
    just because it survived this transform.

    Args:
        name: The candidate name. Only an exact ``str`` can pass.

    Returns:
        A bounded ``snake_case`` code, or ``None`` when nothing survives.
    """
    if type(name) is not str:
        return None
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    code = re.sub(r"[^a-z0-9]+", "_", split.lower()).strip("_")
    return code[:_MAX_REASON_CODE_CHARS].rstrip("_") or None


def _builtin_failure_codes() -> frozenset[str]:
    """Normalize every built-in exception name, so the interpreter's own names are known.

    Derived from :mod:`builtins` rather than transcribed: the source is the
    running interpreter, not a producer, so it carries the same provenance a
    hand-written entry would and cannot drift from the Python version in use.
    """
    codes = set()
    for name in dir(builtins):
        candidate = getattr(builtins, name, None)
        if type(candidate) is type and issubclass(candidate, BaseException):
            code = normalize_reason_code(name)
            if code is not None:
                codes.add(code)
    return frozenset(codes)


# Every ``*Error`` / ``*Exception`` this package defines, normalized the way
# ``bare_error_audit_record`` normalizes the class name of a failure it caught.
# Adding an exception class means adding its code here, or the audit keeps the
# digest of the failure name instead of the name.
_ZEROTH_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "agent_content_blocked_error",
        "agent_input_validation_error",
        "agent_output_validation_error",
        "agent_provider_error",
        "agent_retry_exhausted_error",
        "agent_runner_factory_error",
        "agent_runtime_error",
        "agent_timeout_error",
        "approval_requires_thread_error",
        "duplicate_tool_execution_error",
        "artifact_not_found_error",
        "artifact_storage_error",
        "artifact_store_error",
        "artifact_ttlerror",
        "audit_chain_ordering_error",
        "authentication_error",
        "branch_error",
        "branch_resolution_error",
        "budget_exceeded_error",
        "capability_denied_error",
        "circuit_open_error",
        "clitool_error",
        "clitool_output_error",
        "clitool_process_error",
        "clitool_timeout_error",
        "compaction_error",
        "condition_evaluation_error",
        "context_window_error",
        "contract_not_found_error",
        "contract_registry_error",
        "contract_type_resolution_error",
        "contract_version_exists_error",
        "coordination_timeout_error",
        "deployment_bootstrap_error",
        "deployment_error",
        "deployment_ref_lineage_conflict_error",
        "duplicate_audit_id_error",
        "duplicate_jsonkey_error",
        "econ_threshold_error",
        "eval_threshold_error",
        "executable_unit_admission_error",
        "executable_unit_error",
        "executable_unit_execution_error",
        "executable_unit_input_error",
        "executable_unit_not_found_error",
        "execution_ioerror",
        "fan_out_validation_error",
        "gateway_context_error",
        "gateway_error",
        "governance_context_error",
        "graph_lifecycle_error",
        "graph_validation_error",
        "http_client_error",
        "http_rate_limit_error",
        "http_retry_exhausted_error",
        "input_injection_error",
        "join_reduction_claim_changed_error",
        "join_reduction_recovery_error",
        "join_reduction_release_error",
        "lease_fencing_rejected",
        "lease_lost",
        "legal_hold_error",
        "loop_reduction_claim_changed_error",
        "loop_reduction_recovery_error",
        "loop_reduction_release_error",
        "manifest_validation_error",
        "mapping_execution_error",
        "mapping_validation_error",
        "memory_binding_resolution_error",
        "merge_strategy_error",
        "merge_strategy_validation_error",
        "multiple_branch_pause_error",
        "node_dispatcher_error",
        "orchestrator_error",
        "output_conversion_error",
        "output_extraction_error",
        "parallel_execution_error",
        "parallel_step_limit_error",
        "probe_too_large_error",
        "reducer_ref_validation_error",
        "repo_manifest_validation_error",
        "sandbox_backend_unavailable_error",
        "sandbox_policy_violation_error",
        "sandbox_timeout_error",
        "secret_provider_config_error",
        "secret_resolution_error",
        "signing_config_error",
        "stale_cleanup_claim_error",
        "state_concurrency_error",
        "subgraph_cycle_error",
        "subgraph_depth_limit_error",
        "subgraph_error",
        "subgraph_execution_error",
        "subgraph_resolution_error",
        "template_error",
        "template_not_found_error",
        "template_render_error",
        "template_syntax_validation_error",
        "template_version_exists_error",
        "tenant_scope_error",
        "token_count_error",
        "token_join_transition_error",
        "token_loop_transition_error",
        "token_post_commit_error",
        "token_runtime_unsupported_error",
        "token_scheduler_transition_error",
        "token_snapshot_concurrency_error",
        "token_snapshot_corruption_error",
        "token_snapshot_transition_error",
        "token_snapshot_write_disabled_error",
        "tool_attachment_error",
        "tool_error",
        "tool_execution_error",
        "tool_governance_error",
        "tool_permission_error",
        "tool_validation_error",
        "undeclared_tool_error",
        "unstable_tool_identity_error",
        "unsupported_runtime_adapter_error",
        "upstream_credential_unavailable_error",
        "vault_auth_error",
        "web_socket_bridge_finished_error",
        "web_socket_client_error",
        "web_socket_gateway_close_error",
    }
)

# Denial branches: named verdicts a governance stage returns, not failures. Each
# is a literal in the module that decides it (``AdmissionController.admit``, the
# gateway's admission stage, the capability check), so the set is closed by the
# same edit that would add a branch.
_DENIAL_REASON_CODES: frozenset[str] = frozenset(
    {
        "budget_denied",
        "budget_exceeded",
        "capability_denied",
        # ZER-37 repository-unit stage verdicts: why a repo run was refused
        # (or its smoke stage judged failed) before or after execution.
        "checkout_unavailable_error",
        "classifier_unavailable",
        "command_not_allowed",
        "concurrency",
        "no_trusted_digest_registered",
        "enforcement_unavailable",
        "installation_revoked",
        "policy_denied",
        "policy_unavailable",
        "policy_violation",
        "runtime_not_allowed",
        "script_not_declared",
        "smoke_assertion_failed",
        "trusted_digest",
        "trusted_digest_mismatch",
        "unknown_action",
        "unknown_error",
    }
)
_DECISION_REASON_CODES: frozenset[str] = frozenset(
    {"allowed", "approval_required", "policy_version_mismatch"}
)

REASON_CODES: frozenset[str] = (
    _ZEROTH_FAILURE_CODES | _DENIAL_REASON_CODES | _DECISION_REASON_CODES | _builtin_failure_codes()
)

# Every key the projection retains as readable text, with the closed set of
# terms it may hold. Some sets mirror enums this module cannot import:
# ``SandboxStrictnessMode`` lives in ``integrations`` and ``GovernanceLevel`` in
# the gateway package, and ``governance`` may import only ``contracts`` and
# ``platform`` (``zeroth/_architecture.py``). Those are transcribed by hand.
# A transcription that drifts fails *open* in the direction that matters -- a new
# enum member is summarized away, so a real decision silently stops being
# retained -- so ``tests/governance/audit/test_capture_vocabulary.py`` imports
# both sides (tests are not bound by the layering rule) and pins each mirror
# against its source.
METADATA_VOCABULARIES: Mapping[str, frozenset[str]] = {
    # OperationState and SideEffectSupport. Enumerable by construction, so a
    # vocabulary is the right kind: an unknown term is a producer bug and gets
    # summarized rather than retained.
    "operation_state": frozenset({"not_started", "in_flight", "completed", "failed", "ambiguous"}),
    "operation_support": frozenset({"idempotent", "outcome_queryable", "at_least_once"}),
    # PolicyDecision plus the approval verdicts ApprovalDecisionType mints, plus
    # ``require_approval``: the tool-enforcement verdict for "this call is
    # waiting on a human". ``approve``/``reject`` are ApprovalDecisionType
    # *resolutions* -- what somebody answered -- so neither can stand in for a
    # pending request, and without a term of its own the one verdict that pauses
    # a run is the one whose record loses it.
    "decision": frozenset({"allow", "deny", "approve", "reject", "require_approval"}),
    # RouteDisposition (gateway routing) plus the provider-cache outcomes the
    # agent serializer promotes and the econ waste analyzer reads back.
    "disposition": frozenset({"governed", "transparent", "unsupported", "cache_hit", "cache_miss"}),
    # GovernanceLevel.
    "governance_level": frozenset({"admission", "observed", "enforced"}),
    # The network postures a policy definition may declare. The first four are
    # the exact terms the execution runner reads as "no network".
    "network_mode": frozenset({"disabled", "deny", "none", "off", "allow", "restricted"}),
    # The ``node_type`` discriminators of the graph node union in ``contracts``.
    "node_kind": frozenset(
        {
            "entrypoint",
            "agent",
            "executable_unit",
            "human_approval",
            "subgraph",
            "retrieval",
            "http_request",
        }
    ),
    "reason_code": REASON_CODES,
    # SandboxStrictnessMode.
    "sandbox_strictness_mode": frozenset({"permissive", "standard", "strict"}),
    "webhook_event_type": frozenset(
        {
            "run.completed",
            "run.failed",
            "approval.requested",
            "approval.resolved",
            "approval.escalated",
        }
    ),
    "webhook_transition": frozenset(
        {
            "create_authorized",
            "deactivate_authorized",
            "subscription_created",
            "subscription_deactivated",
            "replay_authorized",
            "delivery_enqueued",
            "delivery_delivered",
            "delivery_failed",
            "delivery_dead_lettered",
        }
    ),
    "template_transition": frozenset({"created", "deleted"}),
    "context_compaction_strategy": frozenset(
        {"observation_masking", "truncation", "llm_summarization"}
    ),
}


__all__ = [
    "ALLOWED_METADATA_KEYS",
    "METADATA_KINDS",
    "METADATA_VOCABULARIES",
    "REASON_CODES",
    "MetadataKind",
    "normalize_reason_code",
]
