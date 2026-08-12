from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from zeroth.econ.plane.connectors.schemas import ConnectorHealthResult, ConnectorSendResult
from zeroth.platform.primitives.boundary import (
    confine_path,
    resolve_outbound_url,
    validate_outbound_url,
)


class ConnectorAdapter(ABC):
    @abstractmethod
    def connector_type(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        raise NotImplementedError

    def healthcheck(self, config: dict[str, Any]) -> ConnectorHealthResult:
        self.validate_config(config)
        return ConnectorHealthResult(healthy=True, message="ok")


class HttpJsonAdapter(ConnectorAdapter):
    endpoint_field = "endpoint"
    auth_header_field = "auth_header"

    def validate_config(self, config: dict[str, Any]) -> None:
        if not config.get(self.endpoint_field):
            raise ValueError(
                f"{self.connector_type()} requires '{self.endpoint_field}' in config_json"
            )
        # A01-34: the endpoint is operator-supplied and reached from inside the
        # deployment's network position. Refused here, at validation, so the
        # socket is never opened -- ``send`` and ``healthcheck`` both route
        # through this method.
        validate_outbound_url(str(config[self.endpoint_field]), context=self.connector_type())

    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        self.validate_config(config)
        endpoint = str(config[self.endpoint_field])
        approved = resolve_outbound_url(endpoint, context=self.connector_type())
        timeout = float(config.get("timeout_s", 5.0))
        headers = {"Content-Type": "application/json"}
        headers["Host"] = approved.host_header
        if config.get(self.auth_header_field):
            headers["Authorization"] = str(config[self.auth_header_field])
        body = {"event_type": event_type, "payload": payload}
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                approved.connect_url,
                json=body,
                headers=headers,
                follow_redirects=False,
                extensions={"sni_hostname": approved.sni_hostname},
            )
        return ConnectorSendResult(
            success=200 <= resp.status_code < 300,
            status_code=resp.status_code,
            response_excerpt=resp.text[:256],
        )


class LangfuseAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "langfuse"


class PosthogAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "posthog"


class SegmentAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "segment"


class SnowplowAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "snowplow"


class LaunchDarklyAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "launchdarkly"


class EnvoyAdapter(HttpJsonAdapter):
    def connector_type(self) -> str:
        return "envoy"


class KafkaAdapter(ConnectorAdapter):
    def connector_type(self) -> str:
        return "kafka"

    def validate_config(self, config: dict[str, Any]) -> None:
        if not config.get("bootstrap_servers"):
            raise ValueError("kafka requires 'bootstrap_servers'")

    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        self.validate_config(config)
        try:
            from kafka import KafkaProducer  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("kafka-python not installed") from exc

        topic_map = config.get("topic_map") or {}
        topic = str(topic_map.get(event_type, config.get("default_topic", "ecp.events")))
        producer = KafkaProducer(
            bootstrap_servers=config["bootstrap_servers"],
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            linger_ms=int(config.get("linger_ms", 5)),
        )
        future = producer.send(
            topic,
            key=str(payload.get("event_key", "")).encode("utf-8"),
            value=payload,
        )
        meta = future.get(timeout=float(config.get("timeout_s", 5.0)))
        producer.flush(timeout=float(config.get("timeout_s", 5.0)))
        producer.close()
        return ConnectorSendResult(
            success=True,
            status_code=0,
            response_excerpt=f"topic={meta.topic} offset={meta.offset}",
        )


class WarehouseFileAdapter(ConnectorAdapter):
    """Lightweight fallback exporter for analytics stores.

    This writes JSONL rows to a local spool path to keep the adapter functional
    in MVP environments without native warehouse SDK credentials.
    """

    adapter_name = "warehouse"

    def connector_type(self) -> str:
        return self.adapter_name

    def _spool_root(self) -> Path:
        """The one directory this adapter may write into.

        Read at call time rather than construction: the registry is built once
        at import, and an operator changing ``ECP_CONNECTOR_SPOOL_ROOT`` should
        not need the adapter rebuilt to take effect.
        """
        from zeroth.econ.plane.config import settings

        return Path(settings.connector_spool_root)

    def validate_config(self, config: dict[str, Any]) -> None:
        if not config.get("spool_path"):
            raise ValueError(f"{self.adapter_name} requires 'spool_path'")
        # A01-34: ``spool_path`` used to be opened verbatim, so it could name any
        # file this process could write. Confined to a declared root, resolved
        # first so ``..`` and symlinks are both caught.
        confine_path(
            str(config["spool_path"]),
            root=self._spool_root(),
            context=self.adapter_name,
        )

    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        self.validate_config(config)
        path = confine_path(
            str(config["spool_path"]),
            root=self._spool_root(),
            context=self.adapter_name,
        )
        row = json.dumps({"event_type": event_type, "payload": payload})
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(row + "\n")
        return ConnectorSendResult(success=True, status_code=0, response_excerpt="spooled")


class ClickHouseAdapter(WarehouseFileAdapter):
    adapter_name = "clickhouse"


class BigQueryAdapter(WarehouseFileAdapter):
    adapter_name = "bigquery"


class SnowflakeAdapter(WarehouseFileAdapter):
    adapter_name = "snowflake"


class NoopAdapter(ConnectorAdapter):
    def __init__(self, name: str) -> None:
        self.name = name

    def connector_type(self) -> str:
        return self.name

    def validate_config(self, config: dict[str, Any]) -> None:
        del config

    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        del event_type, payload, config
        return ConnectorSendResult(success=True, status_code=0, response_excerpt="noop")


def build_adapter_registry() -> dict[str, ConnectorAdapter]:
    return {
        "langfuse": LangfuseAdapter(),
        "posthog": PosthogAdapter(),
        "segment": SegmentAdapter(),
        "snowplow": SnowplowAdapter(),
        "launchdarkly": LaunchDarklyAdapter(),
        "envoy": EnvoyAdapter(),
        "kafka": KafkaAdapter(),
        "clickhouse": ClickHouseAdapter(),
        "bigquery": BigQueryAdapter(),
        "snowflake": SnowflakeAdapter(),
        "prometheus": NoopAdapter("prometheus"),
        "otel_metrics": NoopAdapter("otel_metrics"),
    }
