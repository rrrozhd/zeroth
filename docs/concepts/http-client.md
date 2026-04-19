# Resilient HTTP Client

*Added in v4.0*

The platform-provided async HTTP client gives agent tools and execution units a resilient way to make external HTTP calls with automatic retry, circuit breaking, rate limiting, and connection pooling.

## How It Works

The `ResilientHttpClient` wraps `httpx.AsyncClient` with four resilience layers: configurable retry with exponential backoff and jitter, per-endpoint circuit breakers that open after repeated failures, in-memory token-bucket rate limiting, and shared connection pools with configurable limits. External HTTP calls are gated by policy capabilities, logged in audit records via `HttpCallRecord`, and subject to URL redaction for sensitive parameters.

## Key Components

- **`ResilientHttpClient`** -- Main client class providing `get()`, `post()`, `put()`, `delete()`, and `request()` methods with built-in resilience. Constructed during `bootstrap_service()` and available on the orchestrator.
- **`CircuitBreaker`** -- Per-endpoint circuit breaker with configurable failure threshold and reset timeout. States: closed (normal), open (failing fast), half-open (probing recovery).
- **`CircuitBreakerRegistry`** -- Manages circuit breaker instances per endpoint, creating them on demand with shared configuration.
- **`InMemoryTokenBucket`** -- Token-bucket rate limiter for controlling request throughput per endpoint.
- **`HttpClientSettings`** -- Pydantic settings model configuring retry behavior, circuit breaker thresholds, rate limits, and connection pool sizes.
- **`EndpointConfig`** -- Per-endpoint configuration overriding global defaults for retry, circuit breaking, and authentication.
- **`HttpCallRecord`** -- Audit record capturing URL (redacted), method, status code, timing, and retry count for each external call.
- **`RetryConfig`** -- Configures retry behavior: max attempts, backoff base/max, jitter, and retryable status codes.

## Circuit Breaker States

| State | Behavior |
|-------|----------|
| **Closed** | Normal operation. Failures increment the counter. |
| **Open** | All requests fail fast with `CircuitOpenError`. Entered after failure threshold is reached. |
| **Half-open** | A single probe request is allowed through. Success resets to closed; failure reopens. |

## Security

- External HTTP calls are gated by the `network_read` / `network_write` capabilities in the policy system.
- Auth headers and tokens can be resolved automatically from `SecretResolver` via `EndpointConfig`.
- All external calls are logged in audit records with redacted URLs, status codes, and timing.
- `redact_url()` strips sensitive query parameters and credentials from URLs before logging.

## Error Handling

- **`CircuitOpenError`** -- Raised when a request is rejected because the circuit breaker is open.
- **`HttpRetryExhaustedError`** -- Raised when all retry attempts are exhausted.
- **`HttpRateLimitError`** -- Raised when the token bucket rejects a request due to rate limiting.
- **`HttpClientError`** -- Base error for all HTTP client operations.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.http` for implementation.
