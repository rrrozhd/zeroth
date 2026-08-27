"""Local-only executable-runner adapter for the synthetic evaluation action."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.graph import OperationIdentity
from zeroth.integrations.execution.runner import ExecutableUnitRunResult
from zeroth.platform.artifacts.models import ArtifactReference, generate_artifact_key

from .action_sink import ActionReceipt, ActionSinkFault, EvaluationActionSink

EVALUATION_ACTION_MANIFEST_REF = "evaluation://synthetic-action/v1"
EVALUATION_ACTION_MANIFEST_SHA256 = hashlib.sha256(
    EVALUATION_ACTION_MANIFEST_REF.encode("utf-8")
).hexdigest()
EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF = "evaluation://controlled-failure/v1"
EVALUATION_ARTIFACT_MANIFEST_REF = "evaluation://artifact-emitter/v1"


class EvaluationOutcomeLookupUnavailableError(ConnectionError):
    """A one-shot campaign fault made the local authoritative lookup unavailable."""


class EvaluationActionPayload(BaseModel):
    """The only payload the local evaluation action accepts."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{0,79}$")
    status: Literal["remediated"]
    # Accepted only to prove it has no authority. The adapter never forwards it
    # to the sink and never uses it for lookup or deduplication.
    operation_key: str | None = None
    fault: ActionSinkFault | None = None
    evaluation_behavior: Literal["cancel_after_approval"] | None = None

    def semantic_payload(self) -> dict[str, object]:
        """Return action content without identity or controlled fault controls."""
        return {"ticket": self.ticket, "status": self.status}


class EvaluationActionOutput(BaseModel):
    """Receipt contract exposed to Studio preflight for the local action."""

    model_config = ConfigDict(extra="forbid")

    operation_key: str
    payload_hash: str
    receipt: str
    created_at: str


class EvaluationArtifactPayload(BaseModel):
    """Bounded, deterministic artifact fixture requested by a workflow node."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["json", "text", "image", "binary"]
    label: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    size_bytes: int | None = Field(default=None, ge=1, le=2_000_000)
    ttl_seconds: int = Field(default=3600, ge=1, le=86_400)


class EvaluationArtifactOutput(BaseModel):
    """Workflow-visible reference to bytes held by the tenant artifact store."""

    model_config = ConfigDict(extra="forbid")

    label: str
    artifact: ArtifactReference


def _artifact_bytes(payload: EvaluationArtifactPayload) -> tuple[bytes, str]:
    if payload.kind == "json":
        return (
            json.dumps(
                {"label": payload.label, "source": "zeroth-live-evaluation", "valid": True},
                sort_keys=True,
            ).encode("utf-8"),
            "application/json",
        )
    if payload.kind == "text":
        return f"Zeroth artifact fixture: {payload.label}\n".encode(), "text/plain"
    if payload.kind == "image":
        # Deterministic 1x1 PNG. The payload is synthetic and contains no provider data.
        return (
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            "image/png",
        )
    size = payload.size_bytes or 1_200_000
    return (b"ZEROTH-EVALUATION-BINARY\0" * ((size // 25) + 1))[:size], "application/octet-stream"


def _output(receipt: ActionReceipt) -> dict[str, Any]:
    """Build the stable output shared by first execution, replay and lookup."""
    return {
        "operation_key": receipt.operation_key,
        "payload_hash": receipt.payload_hash,
        "receipt": receipt.receipt,
        "created_at": receipt.created_at,
    }


class EvaluationActionRunner:
    """Intercept one evaluation manifest and delegate every ordinary unit."""

    def __init__(
        self,
        *,
        delegate: Any,
        sink: EvaluationActionSink,
        fault_state: Any | None = None,
        campaign_id: str | None = None,
        artifact_store: Any | None = None,
    ) -> None:
        self.delegate = delegate
        self.sink = sink
        self.fault_state = fault_state
        self.campaign_id = campaign_id
        self.artifact_store = artifact_store

    def __getattr__(self, name: str) -> Any:
        """Preserve the wrapped runner's non-intercepted execution surface."""
        return getattr(self.delegate, name)

    @property
    def secret_resolver(self) -> Any:
        return getattr(self.delegate, "secret_resolver", None)

    @secret_resolver.setter
    def secret_resolver(self, value: Any) -> None:
        self.delegate.secret_resolver = value

    def declares_side_effect(self, manifest_ref: str) -> bool | None:
        if manifest_ref == EVALUATION_ACTION_MANIFEST_REF:
            return True
        if manifest_ref == EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF:
            return False
        if manifest_ref == EVALUATION_ARTIFACT_MANIFEST_REF:
            return True
        probe = getattr(self.delegate, "declares_side_effect", None)
        return None if not callable(probe) else probe(manifest_ref)

    async def run(
        self,
        manifest_ref: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any] | None = None,
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        if manifest_ref == EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF:
            raise RuntimeError("controlled evaluation branch failure")
        if manifest_ref == EVALUATION_ARTIFACT_MANIFEST_REF:
            if not isinstance(operation_identity, OperationIdentity):
                raise ValueError("evaluation artifact emission requires an OperationIdentity")
            if self.artifact_store is None:
                raise RuntimeError("evaluation artifact store is not configured")
            validated = EvaluationArtifactPayload.model_validate(payload)
            data, content_type = _artifact_bytes(validated)
            key = generate_artifact_key(operation_identity.run_id, "artifact-emitter")
            reference = await self.artifact_store.store(
                key,
                data,
                content_type,
                ttl=validated.ttl_seconds,
            )
            output = EvaluationArtifactOutput(label=validated.label, artifact=reference)
            return ExecutableUnitRunResult(
                manifest_ref=EVALUATION_ARTIFACT_MANIFEST_REF,
                input_data=validated.model_dump(mode="json"),
                output_data=output.model_dump(mode="json"),
                audit_record={
                    "manifest_ref": EVALUATION_ARTIFACT_MANIFEST_REF,
                    "artifact_count": 1,
                    "artifact_size": len(data),
                    "artifact_content_type": content_type,
                    "cost_usd": 0.0,
                    "estimated_cost_usd": 0.0,
                    "cost_measurement": "measured",
                },
            )
        if manifest_ref != EVALUATION_ACTION_MANIFEST_REF:
            return await self.delegate.run(
                manifest_ref,
                payload,
                enforcement_context=enforcement_context,
                operation_identity=operation_identity,
            )
        if not isinstance(operation_identity, OperationIdentity):
            raise ValueError("evaluation action requires an OperationIdentity")
        if operation_identity.target_ref != EVALUATION_ACTION_MANIFEST_REF:
            raise ValueError("OperationIdentity target_ref does not match evaluation manifest")

        validated = EvaluationActionPayload.model_validate(payload)
        semantic_payload = validated.semantic_payload()
        fault = validated.fault
        if self.fault_state is not None and self.campaign_id is not None:
            armed = self.fault_state.consume(
                campaign_id=self.campaign_id,
                target="action_sink",
            )
            if armed is not None:
                if armed.mode not in {"unavailable", "timeout_after_commit"}:
                    raise ValueError("unsupported evaluation action-sink fault")
                if fault is not None and fault != armed.mode:
                    raise ValueError("armed action-sink fault does not match run payload")
                fault = armed.mode
        try:
            receipt = self.sink.execute(
                operation_identity.operation_key,
                semantic_payload,
                fault=fault,
            )
        except Exception as error:
            error.audit_record = {  # type: ignore[attr-defined]
                "manifest_ref_sha256": EVALUATION_ACTION_MANIFEST_SHA256,
                "cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "cost_measurement": "measured",
            }
            raise
        return ExecutableUnitRunResult(
            manifest_ref=EVALUATION_ACTION_MANIFEST_REF,
            input_data=semantic_payload,
            output_data=_output(receipt),
            audit_record={
                "manifest_ref_sha256": EVALUATION_ACTION_MANIFEST_SHA256,
                "operation_key": operation_identity.operation_key,
                "evaluation_action_duplicate": receipt.duplicate,
                "cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "cost_measurement": "measured",
            },
        )

    async def outcome_lookup(self, identity: OperationIdentity) -> str | None:
        """Return serialized prior output in NodeDispatcher lookup form."""
        if identity.target_ref != EVALUATION_ACTION_MANIFEST_REF:
            return None
        if self.fault_state is not None and self.campaign_id is not None:
            armed = self.fault_state.consume(
                campaign_id=self.campaign_id,
                target="action_outcome_lookup",
            )
            if armed is not None:
                if armed.mode != "unavailable":
                    raise ValueError("unsupported evaluation action outcome-lookup fault")
                raise EvaluationOutcomeLookupUnavailableError(
                    "controlled evaluation action outcome lookup is unavailable"
                )
        receipt = self.sink.lookup(identity.operation_key)
        if receipt is None:
            return None
        return json.dumps(_output(receipt), sort_keys=True)
