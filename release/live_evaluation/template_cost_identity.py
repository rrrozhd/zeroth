"""Read live-template cost identity from the authoritative durable ledgers.

The join deliberately mirrors :class:`AuthoritativeCampaignExporter`: the
reservation ledger owns run/cost/provider identity and the execution ledger
must independently agree on operation, cost, provider request, and cleanup
state.  Optional JSON metadata and raw audit metadata are never identity
sources.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

_IDENTIFIER = re.compile(r"^[^\s\x00-\x1f\x7f]{1,512}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class PersistedCostIdentityError(RuntimeError):
    """The authoritative persisted planes cannot prove one exact identity."""


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PersistedCostIdentityError(f"{label} is missing or malformed")
    return value


class PersistedCostIdentityReader:
    """Callable adapter for ``collect_live_template_observation.cost_identity``.

    SQLite is opened read-only and a transaction pins both ledger reads to one
    snapshot.  The reader performs no network operation and cannot trigger a
    provider call.
    """

    def __init__(
        self,
        *,
        database: Path,
        tenant_id: str,
        campaign_id: str,
        expected_provider: str,
    ) -> None:
        self.database = database.expanduser().resolve(strict=False)
        self.tenant_id = _identifier(tenant_id, label="tenant identity")
        self.campaign_id = _identifier(campaign_id, label="campaign identity")
        if _PROVIDER.fullmatch(expected_provider) is None:
            raise ValueError("expected_provider is malformed")
        self.expected_provider = expected_provider

    def __call__(self, cost_event_id: str, run_id: str) -> Mapping[str, object]:
        requested_cost = _identifier(cost_event_id, label="cost event identity")
        requested_run = _identifier(run_id, label="run identity")
        if not self.database.is_file():
            raise PersistedCostIdentityError("authoritative economics database is missing")
        database_uri = f"file:{quote(str(self.database), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(database_uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                reservations = connection.execute(
                    """SELECT operation_id, run_id, status, cost_event_id,
                              provider_request_id, cleanup_status
                       FROM cost_reservations
                       WHERE tenant_id = ? AND campaign_id = ?
                         AND run_id = ? AND cost_event_id = ?""",
                    (
                        self.tenant_id,
                        self.campaign_id,
                        requested_run,
                        requested_cost,
                    ),
                ).fetchall()
                if len(reservations) != 1:
                    raise PersistedCostIdentityError("reservation identity is missing or ambiguous")
                reservation = reservations[0]
                operation_id = _identifier(reservation["operation_id"], label="operation identity")
                persisted_run = _identifier(reservation["run_id"], label="reservation run identity")
                persisted_cost = _identifier(
                    reservation["cost_event_id"], label="reservation cost identity"
                )
                provider_request_id = _identifier(
                    reservation["provider_request_id"],
                    label="provider request identity",
                )
                cleanup_status = _identifier(
                    reservation["cleanup_status"], label="reservation cleanup identity"
                )
                if reservation["status"] != "committed":
                    raise PersistedCostIdentityError(
                        "committed reservation identity is unavailable"
                    )

                executions = connection.execute(
                    """SELECT operation_id, execution_id, provider_request_id,
                              cleanup_status, model_version
                       FROM execution_events
                       WHERE tenant_id = ? AND campaign_id = ? AND execution_id = ?""",
                    (self.tenant_id, self.campaign_id, requested_cost),
                ).fetchall()
                if len(executions) != 1:
                    raise PersistedCostIdentityError("execution identity is missing or ambiguous")
                execution = executions[0]
                if (
                    execution["operation_id"] != operation_id
                    or execution["execution_id"] != persisted_cost
                    or execution["provider_request_id"] != provider_request_id
                    or execution["cleanup_status"] != cleanup_status
                ):
                    raise PersistedCostIdentityError(
                        "execution identity drifted from the committed reservation"
                    )
                model_version = _identifier(
                    execution["model_version"], label="persisted model identity"
                )
        except sqlite3.DatabaseError as exc:
            raise PersistedCostIdentityError(
                "authoritative economics schema is unavailable"
            ) from exc

        provider, separator, _model = model_version.partition("/")
        if not separator or provider != self.expected_provider:
            raise PersistedCostIdentityError("provider identity drifted from the persisted model")
        if persisted_run != requested_run or persisted_cost != requested_cost:
            raise PersistedCostIdentityError(
                "reservation identity drifted from the requested run/cost pair"
            )
        return {
            "cost_event_id": persisted_cost,
            "run_id": persisted_run,
            "provider": provider,
            "provider_request_id": provider_request_id,
        }
