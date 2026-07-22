"""Bounded, evidence-based Agent Server compatibility detection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx

from zeroth.core.langgraph_gateway.models import (
    CompatibilityResult,
    CompatibilityStatus,
)

DEFAULT_TESTED_LANGGRAPH_VERSIONS = ("1.2.9",)
DEFAULT_TESTED_AGENT_SERVER_VERSIONS = ("0.11.1",)

# Derived from tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json.
# The digest covers only sorted method/path/operationId rows.
EXPECTED_AGENT_SERVER_OPENAPI_FINGERPRINTS: Mapping[str, str] = {
    "0.11.1": "sha256:67a63cf3bb746d3055d5cb4d1c3055acc1149a8e8f1fdeb8f208d8f567775cc8"
}

_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_VERSION_KEYS = ("langgraph_api_version", "agent_server_version", "version")


def _operation_projection(document: Mapping[str, Any]) -> list[list[str]]:
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI document has no paths object")

    operations: list[list[str]] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            raise ValueError("OpenAPI path item is malformed")
        for method, operation in path_item.items():
            normalized_method = str(method).lower()
            if normalized_method not in _HTTP_METHODS:
                continue
            if not isinstance(operation, Mapping):
                raise ValueError("OpenAPI operation is malformed")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("OpenAPI operationId is missing")
            operations.append([normalized_method.upper(), path, operation_id])
    operations.sort(key=lambda row: (row[0], row[1], row[2]))
    return operations


def fingerprint_openapi(document: Mapping[str, Any]) -> str:
    """Hash the stable OpenAPI operation projection, excluding prose/examples."""
    canonical = json.dumps(
        _operation_projection(document), sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class CompatibilityDetector:
    """Probe one Agent Server once and match it against the tested matrix."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        tested_langgraph_versions: tuple[str, ...] = DEFAULT_TESTED_LANGGRAPH_VERSIONS,
        tested_agent_server_versions: tuple[str, ...] = DEFAULT_TESTED_AGENT_SERVER_VERSIONS,
        expected_openapi_fingerprints: Mapping[
            str, str
        ] = EXPECTED_AGENT_SERVER_OPENAPI_FINGERPRINTS,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client = client
        self._tested_langgraph_versions = tested_langgraph_versions
        self._tested_agent_server_versions = tested_agent_server_versions
        self._expected_openapi_fingerprints = dict(expected_openapi_fingerprints)
        self._timeout_seconds = timeout_seconds

    def _result(
        self,
        status: CompatibilityStatus,
        *,
        detected_version: str | None = None,
        openapi_fingerprint: str | None = None,
        reason: str | None = None,
    ) -> CompatibilityResult:
        return CompatibilityResult(
            tested_langgraph_versions=self._tested_langgraph_versions,
            tested_agent_server_versions=self._tested_agent_server_versions,
            detected_agent_server_version=detected_version,
            openapi_fingerprint=openapi_fingerprint,
            status=status,
            reason=reason,
        )

    async def _get(self, path: str) -> httpx.Response:
        return await self._client.get(path, timeout=self._timeout_seconds)

    async def detect(self) -> CompatibilityResult:
        """Probe ``/info``, ``/ok``, and ``/openapi.json`` without retries."""
        try:
            info_response = await self._get("/info")
        except httpx.HTTPError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server is unavailable",
            )

        info: Mapping[str, Any] = {}
        if info_response.status_code < 400:
            try:
                decoded_info = info_response.json()
            except (json.JSONDecodeError, ValueError):
                decoded_info = None
            if not isinstance(decoded_info, Mapping):
                malformed_info = True
            else:
                malformed_info = False
                info = decoded_info
        elif info_response.status_code == 404:
            malformed_info = False
        else:
            malformed_info = True

        try:
            ok_response = await self._get("/ok")
            openapi_response = await self._get("/openapi.json")
        except httpx.HTTPError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server is unavailable",
            )

        detected_version = self._extract_version(info)
        openapi_digest: str | None = None
        openapi_malformed = False
        if openapi_response.status_code < 400:
            try:
                openapi_document = openapi_response.json()
                if not isinstance(openapi_document, Mapping):
                    raise ValueError("OpenAPI response is not an object")
                openapi_digest = fingerprint_openapi(openapi_document)
            except (json.JSONDecodeError, ValueError):
                openapi_malformed = True

        if ok_response.status_code >= 400:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                detected_version=detected_version,
                openapi_fingerprint=openapi_digest,
                reason="upstream Agent Server readiness probe failed",
            )
        if malformed_info:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                openapi_fingerprint=openapi_digest,
                reason="upstream /info response is malformed",
            )
        if (
            detected_version is not None
            and detected_version not in self._tested_agent_server_versions
        ):
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                detected_version=detected_version,
                openapi_fingerprint=openapi_digest,
                reason=f"Agent Server version {detected_version} is not in the tested matrix",
            )
        if openapi_malformed:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                detected_version=detected_version,
                reason="upstream OpenAPI response is malformed",
            )

        known_digests = frozenset(self._expected_openapi_fingerprints.values())
        if openapi_digest is not None and openapi_digest not in known_digests:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                detected_version=detected_version,
                openapi_fingerprint=openapi_digest,
                reason="upstream OpenAPI fingerprint is not in the tested matrix",
            )
        if detected_version is not None or openapi_digest in known_digests:
            return self._result(
                CompatibilityStatus.SUPPORTED,
                detected_version=detected_version,
                openapi_fingerprint=openapi_digest,
                reason="upstream matches the tested compatibility matrix",
            )
        return self._result(
            CompatibilityStatus.UNSUPPORTED,
            reason="upstream version and OpenAPI fingerprint are unknown",
        )

    @staticmethod
    def _extract_version(info: Mapping[str, Any]) -> str | None:
        for key in _VERSION_KEYS:
            version = info.get(key)
            if isinstance(version, str) and version:
                return version
        return None
