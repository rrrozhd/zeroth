"""Verify the persistent synthetic Chroma corpus without exposing embeddings."""

from __future__ import annotations

import hashlib
import json
import urllib.request

BASE = "http://127.0.0.1:8121/api/v2"
TENANT = "default_tenant"
DATABASE = "default_database"
EXPECTED_DOCUMENTS = {
    "evaluation-ground-truth-alpha": (
        '"Synthetic evaluation fact alpha: the launch window is 09:30 UTC."'
    ),
    "evaluation-ground-truth-beta": (
        '"Synthetic evaluation fact beta: the approved queue depth is four."'
    ),
    "evaluation-conflict-beta": (
        '"Synthetic conflicting fact beta: an obsolete note says queue depth is six."'
    ),
}


def _json(path: str, *, payload: dict[str, object] | None = None) -> object:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main() -> int:
    heartbeat = _json("/heartbeat")
    collections = _json(f"/tenants/{TENANT}/databases/{DATABASE}/collections")
    if not isinstance(heartbeat, dict) or not isinstance(collections, list):
        raise RuntimeError("Chroma control-plane response is invalid")
    candidates = [
        item
        for item in collections
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith("zeroth_memory_shared_evaluation_studio_v1_")
        and item["name"].endswith("_shared")
    ]
    if len(candidates) != 1:
        raise RuntimeError("tenant-scoped evaluation collection is not unique")
    collection = candidates[0]
    collection_id = str(collection["id"])
    path = f"/tenants/{TENANT}/databases/{DATABASE}/collections/{collection_id}"
    count = _json(f"{path}/count")
    records = _json(
        f"{path}/get",
        payload={"limit": 10, "include": ["documents", "metadatas"]},
    )
    if not isinstance(records, dict):
        raise RuntimeError("Chroma corpus response is invalid")
    ids = records.get("ids")
    documents = records.get("documents")
    metadatas = records.get("metadatas")
    if not (
        count == 3
        and isinstance(ids, list)
        and isinstance(documents, list)
        and isinstance(metadatas, list)
        and len(ids) == len(documents) == len(metadatas) == 3
    ):
        raise RuntimeError("Chroma corpus cardinality is invalid")
    observed = {str(key): str(document) for key, document in zip(ids, documents, strict=True)}
    if observed != EXPECTED_DOCUMENTS:
        raise RuntimeError("Chroma synthetic corpus content does not match")
    if any(
        not isinstance(metadata, dict)
        or metadata.get("scope") != "shared"
        or metadata.get("target") != "__shared__"
        or metadata.get("key") != key
        for key, metadata in zip(ids, metadatas, strict=True)
    ):
        raise RuntimeError("Chroma corpus scope metadata does not match")

    result = {
        "application_tenant_scope_marker": "evaluation_studio_v1",
        "collection_id": collection_id,
        "collection_name": collection["name"],
        "document_count": count,
        "document_sha256": {
            key: hashlib.sha256(value.encode()).hexdigest()
            for key, value in sorted(observed.items())
        },
        "heartbeat_present": bool(heartbeat),
        "loopback_endpoint": "127.0.0.1:8121",
        "metadata_scope": "shared_within_application_tenant_collection",
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
