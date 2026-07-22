"""Bounded, evidence-based Agent Server compatibility detection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
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
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True, slots=True)
class _ProbeResponse:
    status_code: int
    body: bytes


class _ProbeTooLargeError(Exception):
    pass


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
        info_max_response_bytes: int = 64 * 1024,
        ok_max_response_bytes: int = 8 * 1024,
        openapi_max_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        response_limits = (
            info_max_response_bytes,
            ok_max_response_bytes,
            openapi_max_response_bytes,
        )
        if any(
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            for limit in response_limits
        ):
            raise ValueError("probe response byte limits must be positive integers")
        self._client = client
        self._tested_langgraph_versions = tested_langgraph_versions
        self._tested_agent_server_versions = tested_agent_server_versions
        self._expected_openapi_fingerprints = dict(expected_openapi_fingerprints)
        self._timeout_seconds = timeout_seconds
        self._info_max_response_bytes = info_max_response_bytes
        self._ok_max_response_bytes = ok_max_response_bytes
        self._openapi_max_response_bytes = openapi_max_response_bytes

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

    async def _probe(self, path: str, *, max_response_bytes: int) -> _ProbeResponse:
        async with asyncio.timeout(self._timeout_seconds):
            async with self._client.stream(
                "GET",
                path,
                headers={"accept-encoding": "identity"},
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as response:
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > max_response_bytes:
                        raise _ProbeTooLargeError

                body = bytearray()
                if response.is_stream_consumed:
                    if len(response.content) > max_response_bytes:
                        raise _ProbeTooLargeError
                    body.extend(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > max_response_bytes:
                            raise _ProbeTooLargeError
                        body.extend(chunk)
                return _ProbeResponse(response.status_code, bytes(body))

    async def detect(self) -> CompatibilityResult:
        """Probe ``/info``, ``/ok``, and ``/openapi.json`` without retries."""
        try:
            info_response = await self._probe(
                "/info", max_response_bytes=self._info_max_response_bytes
            )
        except _ProbeTooLargeError:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                reason="upstream /info response exceeds the probe size limit",
            )
        except (TimeoutError, httpx.TimeoutException):
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server probe timed out",
            )
        except httpx.HTTPError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server is unavailable",
            )

        info: Mapping[str, Any] = {}
        if 200 <= info_response.status_code < 300:
            try:
                decoded_info = json.loads(info_response.body)
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
            ok_response = await self._probe("/ok", max_response_bytes=self._ok_max_response_bytes)
        except _ProbeTooLargeError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason=("upstream Agent Server readiness response exceeds the probe size limit"),
            )
        except (TimeoutError, httpx.TimeoutException):
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server probe timed out",
            )
        except httpx.HTTPError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server is unavailable",
            )

        try:
            openapi_response = await self._probe(
                "/openapi.json", max_response_bytes=self._openapi_max_response_bytes
            )
        except _ProbeTooLargeError:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                reason="upstream OpenAPI response exceeds the probe size limit",
            )
        except (TimeoutError, httpx.TimeoutException):
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server probe timed out",
            )
        except httpx.HTTPError:
            return self._result(
                CompatibilityStatus.UNAVAILABLE,
                reason="upstream Agent Server is unavailable",
            )

        detected_version = self._extract_version(info)
        has_version_claim = any(key in info for key in _VERSION_KEYS)
        openapi_digest: str | None = None
        openapi_malformed = False
        if 200 <= openapi_response.status_code < 300:
            try:
                openapi_document = json.loads(openapi_response.body)
                if not isinstance(openapi_document, Mapping):
                    raise ValueError("OpenAPI response is not an object")
                openapi_digest = fingerprint_openapi(openapi_document)
            except (json.JSONDecodeError, ValueError):
                openapi_malformed = True

        if not 200 <= ok_response.status_code < 300:
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
                reason="detected Agent Server version is not in the tested matrix",
            )
        if openapi_malformed:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                detected_version=detected_version,
                reason="upstream OpenAPI response is malformed",
            )

        if has_version_claim and detected_version is None:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                openapi_fingerprint=openapi_digest,
                reason="upstream version and OpenAPI evidence conflict",
            )
        if detected_version is not None:
            expected_digest = self._expected_openapi_fingerprints.get(detected_version)
            if openapi_digest is not None and openapi_digest != expected_digest:
                return self._result(
                    CompatibilityStatus.UNSUPPORTED,
                    detected_version=detected_version,
                    openapi_fingerprint=openapi_digest,
                    reason="upstream version and OpenAPI evidence conflict",
                )
            return self._result(
                CompatibilityStatus.SUPPORTED,
                detected_version=detected_version,
                openapi_fingerprint=openapi_digest,
                reason="upstream matches the tested compatibility matrix",
            )
        known_digests = frozenset(self._expected_openapi_fingerprints.values())
        if openapi_digest in known_digests:
            return self._result(
                CompatibilityStatus.SUPPORTED,
                openapi_fingerprint=openapi_digest,
                reason="upstream matches the tested compatibility matrix",
            )
        if openapi_digest is not None:
            return self._result(
                CompatibilityStatus.UNSUPPORTED,
                openapi_fingerprint=openapi_digest,
                reason="upstream OpenAPI fingerprint is not in the tested matrix",
            )
        return self._result(
            CompatibilityStatus.UNSUPPORTED,
            reason="upstream version and OpenAPI fingerprint are unknown",
        )

    @staticmethod
    def _extract_version(info: Mapping[str, Any]) -> str | None:
        for key in _VERSION_KEYS:
            version = info.get(key)
            if (
                isinstance(version, str)
                and len(version) <= 64
                and _VERSION_PATTERN.fullmatch(version) is not None
            ):
                return version
        return None
