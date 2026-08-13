"""Async database-backed storage for audit records.

Provides the AuditRepository class that handles saving and querying
NodeAuditRecord objects using an async database.

**This is the capture boundary, and it is the only one.**
:meth:`AuditRepository.write` is the single durable chokepoint -- ``write_many``
delegates to it -- so :mod:`zeroth.governance.audit.capture_policy` is applied
*here*, on every record, unconditionally, before the digest is computed. The
delivery worker used to apply it too, which left the second pass needing to
recognise the first pass's work or destroy it; the only channel for that was a
marker in producer-supplied ``execution_metadata``, forgeable by hand and
detachable from the content it described. Nothing on the record is consulted
now, so nothing on it can be forged.

**The trade-off, stated plainly:** capture protects every *producer* -- all
thirteen audit call sites reach this repository, including the orchestration
runtime writing node prompts, results, errors and denials -- but not a
hypothetical non-repository :class:`AuditRecordWriter` injected into
:class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue`. Production injects
only this class (``core/langgraph_gateway/events.py``), and that Protocol now
requires an implementation to be a capture-applying durable sink.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.governance.audit.coordination import (
    advance_audit_chain,
    hydrate_audit_row,
    load_ordered_run_records,
    lock_audit_chain,
    order_audit_records,
)
from zeroth.governance.audit.erasure_schema import (
    ERASED_PII_VALUES,
    LATEST_DIGEST_VERSION,
    pii_commitment_fields,
)
from zeroth.governance.audit.errors import DuplicateAuditIdError
from zeroth.governance.audit.models import AuditQuery, NodeAuditRecord
from zeroth.governance.audit.verifier import _compute_pii_commitments, compute_chained_record
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncConnection,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
    TenantWideScopeContext,
)
from zeroth.platform.storage.json import to_json_value
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    named_isolation_probe,
    persistence_operation,
    persistence_resource_operations,
    persistence_surface,
)

if TYPE_CHECKING:
    from zeroth.governance.audit.capture_policy import CaptureClassifier
    from zeroth.platform.signing import SigningKeyProvider


@persistence_surface(
    "service.audit_chain_heads",
    probe=named_isolation_probe(
        "zeroth.service.audit_isolation_probe:_drive_audit_chain_heads"
    ),
    non_persistence_public_methods=frozenset({"configure_capture"}),
    method_names=frozenset({"write", "write_many"}),
)
@persistence_surface(
    "service.node_audits",
    probe=named_isolation_probe("zeroth.service.audit_isolation_probe:_drive_node_audits"),
    non_persistence_public_methods=frozenset({"configure_capture"}),
)
class AuditRepository:
    """Saves and retrieves audit records from an async database.

    Use this class to store audit records when nodes run and to look them
    up later for debugging, compliance, or building timelines.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext | TenantWideScopeContext,
        signer: SigningKeyProvider | None = None,
    ):
        if type(scope_context) not in {
            ScopeContext,
            NullWorkspaceScopeContext,
            TenantWideScopeContext,
        }:
            raise TypeError("scope_context must be a trusted scope context")
        self._database: AsyncDatabase = database
        self._scope_context = scope_context
        table = (
            ScopedTable
            if type(scope_context) in {ScopeContext, NullWorkspaceScopeContext}
            else ScopedTable.for_privileged_tenant_wide
        )
        self._audits = table(database, SERVICE_SCOPE_REGISTRY, "service.node_audits", scope_context)
        self._chain_heads = table(
            database, SERVICE_SCOPE_REGISTRY, "service.audit_chain_heads", scope_context
        )
        # WS-D signer: signs each record's digest under the SAME chain lock that
        # fixes the chain head, so the digest and its signature are committed
        # atomically. None -> records stay unsigned-legacy (injected post-build
        # by bootstrap once the shared secret provider exists).
        self._signer = signer
        # Constructed, never accepted: a capture boundary a caller can replace
        # with a pass-through is not a boundary. Only the classifier is
        # configurable, via ``configure_capture``.
        self._capture = AuditCapturePolicy()
        self._capture_configured = False

    @classmethod
    def scoped(
        cls,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext | TenantWideScopeContext,
        signer: SigningKeyProvider | None = None,
    ) -> AuditRepository:
        """Construct an audit repository bound to one trusted tenant/workspace."""
        return cls(database, scope_context, signer)

    @classmethod
    def for_default_compatibility(
        cls,
        database: AsyncDatabase,
        *,
        signer: SigningKeyProvider | None = None,
    ) -> AuditRepository:
        """Bind legacy tests and migration tools to the reserved default scope."""
        return cls(
            database,
            NullWorkspaceScopeContext.for_default_compatibility(),
            signer,
        )

    def _validate_owner(self, tenant_id: str, workspace_id: str | None) -> None:
        if tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        if (
            type(self._scope_context) is ScopeContext
            and workspace_id != self._scope_context.workspace_id
        ):
            raise ValueError("workspace_id does not match bound scope")
        if type(self._scope_context) is NullWorkspaceScopeContext and workspace_id is not None:
            raise ValueError("workspace_id does not match bound scope")

    def configure_capture(self, classifier: CaptureClassifier) -> None:
        """Install the deployment's capture classifier, once, at wiring time.

        The classifier is the *only* replaceable part of the capture boundary:
        it picks between two fixed outcomes and cannot author either, so a
        deployment can opt into retaining content without supplying the
        transform that decides what "retained" means.

        Args:
            classifier: Decides per record whether content may be retained.

        Raises:
            ValueError: If a classifier was already installed. Capture posture
                is wiring, not a runtime switch: a repository whose policy can
                be swapped mid-flight has no posture at all.
        """
        if self._capture_configured:
            raise ValueError("audit capture classifier is already configured")
        self._capture = AuditCapturePolicy(classifier=classifier)
        self._capture_configured = True

    @persistence_resource_operations(
        "service.audit_chain_heads",
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.UPDATE,
    )
    @persistence_resource_operations(
        "service.node_audits", ResourceOperation.CREATE, ResourceOperation.READ
    )
    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Save an audit record to the database.

        Writes are append-only. Duplicate audit IDs are rejected so history
        cannot be silently rewritten. The record is classified and redacted
        first -- always, with no way for a caller to signal otherwise -- so what
        is digested and inserted is what the capture policy allows.

        Raises:
            DuplicateAuditIdError: If ``record.audit_id`` is already stored --
                and only then. It subclasses ``ValueError`` so callers catching
                ``ValueError`` are unaffected, while one that treats "already
                stored" as a successful delivery can narrow to the exact type
                instead of reading every pre-commit failure as a durable record.
        """
        # First, so the digest below covers the captured object.
        record = self._capture.apply(record)
        self._validate_owner(record.tenant_id, record.workspace_id)
        async with self._audits.transaction(write_lock=True) as audits:
            heads = audits.bind(self._chain_heads)
            head = await lock_audit_chain(
                audits,
                heads,
                run_id=record.run_id,
            )
            # WS-E: stamp the latest commitment digest version BEFORE the
            # digest is computed, so the digest folds in the commitments and stays
            # identical after a later crypto-erasure nulls the plaintext. Always
            # populated for a commitment write — never left None (an empty
            # record would still "verify" while binding no PII).
            versioned = record.model_copy(update={"digest_version": LATEST_DIGEST_VERSION})
            prepared = versioned.model_copy(
                update={
                    "pii_commitments": _compute_pii_commitments(versioned),
                    "chain_sequence": head.next_sequence,
                }
            )
            chained = compute_chained_record(
                prepared,
                head.digest,
                self._signer,
            )
            existing = await audits.select_one(
                where={"audit_id": chained.audit_id},
                columns=("audit_id",),
            )
            if existing is not None:
                raise DuplicateAuditIdError(f"audit_id {record.audit_id!r} already exists")
            created_at = datetime.now(UTC)
            await audits.insert(
                {
                    "audit_id": chained.audit_id,
                    "run_id": chained.run_id,
                    "thread_id": chained.thread_id,
                    "node_id": chained.node_id,
                    "graph_version_ref": chained.graph_version_ref,
                    "deployment_ref": chained.deployment_ref,
                    "tenant_id": chained.tenant_id,
                    "workspace_id": chained.workspace_id,
                    "created_at": created_at.isoformat(),
                    "chain_sequence": chained.chain_sequence,
                    "record_json": to_json_value(chained.model_dump(mode="json")),
                }
            )
            if chained.record_digest is None:  # pragma: no cover - compute contract
                raise RuntimeError("audit record digest was not computed")
            await advance_audit_chain(
                heads,
                run_id=record.run_id,
                digest=chained.record_digest,
                next_sequence=head.next_sequence + 1,
            )
        return await self.get(record.audit_id)

    @persistence_operation(ResourceOperation.READ)
    async def get(self, audit_id: str, *, tenant_id: str | None = None) -> NodeAuditRecord | None:
        """Look up one audit record, optionally constrained by tenant in SQL."""
        if tenant_id is not None and tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        row = await self._audits.select_one(
            where={"audit_id": audit_id},
            columns=("record_json", "chain_sequence"),
        )
        if row is None:
            return None
        return self._hydrate(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(
        self, query: AuditQuery | None = None, *, limit: int | None = None
    ) -> list[NodeAuditRecord]:
        """Return audit records matching the given filters, ordered by time.

        Pass an AuditQuery to filter by run, thread, node, etc. If no query
        is given, all records are returned.

        ``limit`` bounds the read to the *most recent* ``limit`` records, still
        returned oldest-first. Without it a caller reads the deployment's entire
        audit history, which is what the econ-analytics and rightsizing routes
        used to do before filtering in Python.
        """
        query = query or AuditQuery()
        if query.tenant_id is not None and query.tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        if (
            type(self._scope_context) is ScopeContext
            and query.workspace_id is not None
            and query.workspace_id != self._scope_context.workspace_id
        ):
            raise ValueError("workspace_id does not match bound scope")
        if (
            type(self._scope_context) is NullWorkspaceScopeContext
            and query.workspace_id is not None
        ):
            raise ValueError("workspace_id does not match bound scope")
        if (
            type(self._scope_context) is ScopeContext
            and query.workspace_scoped
            and query.workspace_id is None
        ):
            raise ValueError("workspace_id does not match bound scope")
        where: dict[str, str] = {}
        for field in (
            "run_id",
            "thread_id",
            "node_id",
            "graph_version_ref",
            "deployment_ref",
        ):
            value = getattr(query, field)
            if value is None:
                continue
            where[field] = value
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a non-negative int")
        async with self._audits.transaction() as audits:
            if limit is None:
                rows = await audits.select(
                    where=where,
                    columns=("record_json", "chain_sequence"),
                    order_by=("created_at", "audit_id"),
                )
            else:
                # A bound on a time-ordered read has to mean the newest N.
                # Selecting descending and reversing keeps the ascending
                # contract while letting SQLite discard the rest of the history.
                rows = await audits.select(
                    where=where,
                    columns=("record_json", "chain_sequence"),
                    order_by_desc=("created_at", "audit_id"),
                    limit=limit,
                )
                rows = list(reversed(rows))
        records = [self._hydrate(row) for row in rows]
        return order_audit_records(records) if query.run_id is not None else records

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        workspace_scoped: bool = False,
        deployment_ref: str | None = None,
    ) -> list[NodeAuditRecord]:
        """Return all audit records for a specific run."""
        return await self.list(
            AuditQuery(
                run_id=run_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                workspace_scoped=workspace_scoped,
                deployment_ref=deployment_ref,
            )
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_run_in_transaction(
        self,
        connection: AsyncConnection,
        run_id: str,
    ) -> list[NodeAuditRecord]:
        """Return a run's records using the caller's database transaction."""
        return await load_ordered_run_records(self._audits.in_transaction(connection), run_id)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_thread(self, thread_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific thread."""
        return await self.list(AuditQuery(thread_id=thread_id))

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_node(self, node_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific node."""
        return await self.list(AuditQuery(node_id=node_id))

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_graph_version(self, graph_version_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific graph version."""
        return await self.list(AuditQuery(graph_version_ref=graph_version_ref))

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_by_deployment(
        self,
        deployment_ref: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        workspace_scoped: bool = False,
    ) -> list[NodeAuditRecord]:
        """Return all audit records for a specific deployment."""
        return await self.list(
            AuditQuery(
                deployment_ref=deployment_ref,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                workspace_scoped=workspace_scoped,
            )
        )

    @persistence_resource_operations(
        "service.audit_chain_heads",
        ResourceOperation.CREATE,
        ResourceOperation.READ,
        ResourceOperation.UPDATE,
    )
    @persistence_resource_operations(
        "service.node_audits", ResourceOperation.CREATE, ResourceOperation.READ
    )
    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def write_many(self, records: Sequence[NodeAuditRecord]) -> list[NodeAuditRecord]:
        """Save multiple audit records at once. Returns all saved records."""
        return [await self.write(record) for record in records]

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def crypto_erase(self, audit_id: str, *, reason: str) -> NodeAuditRecord | None:
        """Crypto-erase a single record's PII while keeping the chain verifiable.

        A SANCTIONED, append-only-preserving single-row UPDATE: it nulls the PII
        payload fields (``input_snapshot``, ``output_snapshot``, ``stdout``, tool
        calls, memory interactions, …), keeps ``pii_commitments`` and the digest,
        and stamps ``erased``/``erased_at``/``erasure_reason``. Because a v2
        digest is computed over the commitments (not the plaintext), the record
        digest, its signature, and the whole hash-chain still verify afterwards.

        ``created_at``, ``audit_id``, ``previous_record_digest`` and
        ``record_digest`` are NEVER touched — re-chaining history is itself a
        tamper event. digest_version=1 (legacy) records are un-erasable and raise;
        an already-erased or missing record is a no-op (idempotent).
        """
        async with self._audits.transaction() as audits:
            return await self.crypto_erase_in_transaction(
                audits,
                audit_id,
                reason=reason,
            )

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def crypto_erase_in_transaction(
        self,
        connection: AsyncConnection | BoundStructuredTable,
        audit_id: str,
        *,
        reason: str,
        record: NodeAuditRecord | None = None,
    ) -> NodeAuditRecord | None:
        """Crypto-erase one audit through an existing transaction."""
        audits = self._audits.in_transaction(connection)
        if record is None:
            row = await audits.select_one(
                where={"audit_id": audit_id},
                columns=("record_json", "chain_sequence"),
            )
            record = None if row is None else self._hydrate(row)
        if record is None:
            return None
        self._validate_owner(record.tenant_id, record.workspace_id)
        if record.audit_id != audit_id:
            raise ValueError(
                f"supplied record audit_id {record.audit_id!r} does not match {audit_id!r}"
            )
        if (record.digest_version or 1) < 2:
            raise ValueError(
                f"audit_id {audit_id!r} is digest_version=1 (legacy) and cannot be "
                "crypto-erased; legacy whole-payload digests are grandfathered"
            )
        expected_commitments = _compute_pii_commitments(record)
        expected_keys = set(pii_commitment_fields(record.digest_version))
        stored_commitments = record.pii_commitments or {}
        if set(stored_commitments) != expected_keys:
            raise ValueError(f"audit_id {audit_id!r} has invalid pii_commitments field schema")
        if not record.erased and stored_commitments != expected_commitments:
            raise ValueError(
                f"audit_id {audit_id!r} has pii_commitments that do not match plaintext"
            )
        if record.digest_version == 2 and (record.condition_results or record.approval_actions):
            raise ValueError(
                f"audit_id {audit_id!r} is digest_version=2 and contains structured "
                "PII that predates expanded commitments; it cannot be crypto-erased"
            )
        if record.erased:
            return record  # idempotent: already erased, commitments/digest intact
        erased = record.model_copy(
            update={
                **ERASED_PII_VALUES,
                "erased": True,
                "erased_at": datetime.now(UTC),
                "erasure_reason": reason,
            }
        )
        await audits.update(
            {"record_json": to_json_value(erased.model_dump(mode="json"))},
            where={"audit_id": audit_id},
        )
        return erased

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_erasable(
        self,
        tenant_id: str,
        older_than: datetime,
        *,
        exclude_run_ids: Sequence[str] | None = None,
    ) -> list[NodeAuditRecord]:
        """Return erasable commitment-digest records older than a cutoff.

        Scoped to one tenant and to records created before ``older_than``
        (compared as UTC isoformat, matching the write path). Legacy v1 records
        are excluded (un-erasable), as are records for any run in
        ``exclude_run_ids`` (legal-hold protected).
        """
        async with self._audits.transaction() as audits:
            return await self.list_erasable_in_transaction(
                audits,
                tenant_id,
                older_than,
                exclude_run_ids=exclude_run_ids,
            )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_erasable_in_transaction(
        self,
        connection: AsyncConnection | BoundStructuredTable,
        tenant_id: str,
        older_than: datetime,
        *,
        exclude_run_ids: Sequence[str] | None = None,
    ) -> list[NodeAuditRecord]:
        """Transaction-scoped :meth:`list_erasable` for coordinated sweeps.

        One cutoff-bounded projection query; only aged rows are hydrated —
        never the tenant's full audit history.
        """
        if tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        audits = self._audits.in_transaction(connection)
        excluded = tuple(dict.fromkeys(exclude_run_ids or ()))
        cutoff = older_than.astimezone(UTC).isoformat()
        rows = await audits.select(
            columns=("record_json", "chain_sequence"),
            where_lt={"created_at": cutoff},
            where_not_in={"run_id": excluded},
            order_by=("created_at", "audit_id"),
        )
        records = [self._hydrate(row) for row in rows]
        return [record for record in records if (record.digest_version or 1) >= 2]

    @staticmethod
    def _hydrate(row: dict[str, object]) -> NodeAuditRecord:
        """Rebuild a :class:`NodeAuditRecord` from a fetched row via the shared hydrator."""
        return hydrate_audit_row(row)
