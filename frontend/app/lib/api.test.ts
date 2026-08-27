// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiFetch,
  deploymentRef,
  getArtifact,
  getManifest,
  listManifestRuns,
  resolveAmbiguousOperation,
} from "./api";
import { setConfig } from "./config";

describe("apiFetch authentication admission", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("fails locally without emitting an unauthenticated backend request", async () => {
    await expect(apiFetch("/v1/audit/events")).rejects.toMatchObject({
      status: 0,
      message: "Connect to the API before loading protected data.",
    });

    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("apiFetch transport recovery", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setConfig("http://127.0.0.1:8122", "test-service-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  it("retries an implicit GET once after a thrown transport failure", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockRejectedValueOnce(new TypeError("stale pooled connection"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ready: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    await expect(apiFetch<{ ready: boolean }>("/v1/identity")).resolves.toEqual({ ready: true });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(fetchMock.mock.calls[1]?.[1]?.signal);
  });

  it("retries an explicit GET once after a thrown transport failure", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockRejectedValueOnce(new TypeError("connection replaced"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ready: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    await expect(
      apiFetch<{ ready: boolean }>("/v1/identity", { method: "get" }),
    ).resolves.toEqual({ ready: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("returns a status-zero error after the single GET retry is exhausted", async () => {
    const fetchMock = vi.mocked(fetch).mockRejectedValue(new TypeError("connection reset"));

    await expect(apiFetch("/v1/runs/run-1/timeline")).rejects.toMatchObject({
      status: 0,
      message:
        "Network error reaching http://127.0.0.1:8122/v1/runs/run-1/timeline: connection reset",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry an HTTP error response", async () => {
    const fetchMock = vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "temporarily unavailable" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(apiFetch("/v1/identity")).rejects.toMatchObject({
      status: 503,
      message: "temporarily unavailable",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry a mutation after a thrown transport failure", async () => {
    const fetchMock = vi.mocked(fetch).mockRejectedValue(new TypeError("connection reset"));

    await expect(
      apiFetch("/v1/runs", { method: "POST", body: JSON.stringify({ input_payload: {} }) }),
    ).rejects.toMatchObject({ status: 0 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry when the caller's signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort(new DOMException("caller cancelled", "AbortError"));
    const fetchMock = vi.mocked(fetch).mockImplementation((_input, init) =>
      Promise.reject(init?.signal?.reason ?? new DOMException("caller cancelled", "AbortError")),
    );

    await expect(apiFetch("/v1/identity", { signal: controller.signal })).rejects.toMatchObject({
      status: 0,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry when the caller aborts during transport backoff", async () => {
    const controller = new AbortController();
    const fetchMock = vi.mocked(fetch).mockRejectedValueOnce(new TypeError("connection reset"));

    const request = apiFetch("/v1/identity", { signal: controller.signal });
    controller.abort(new DOMException("caller cancelled", "AbortError"));

    await expect(request).rejects.toMatchObject({ status: 0 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("deploymentRef", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("re-reads health so a restarted API cannot leave scoped screens on a stale deployment", async () => {
    setConfig("http://127.0.0.1:8122", "test-service-key");
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ deployment_ref: "before-restart" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ deployment_ref: "after-restart" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    await expect(deploymentRef()).resolves.toBe("before-restart");
    await expect(deploymentRef()).resolves.toBe("after-restart");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("getArtifact", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
  });

  it("retrieves bytes with authentication and safe response metadata", async () => {
    setConfig("http://127.0.0.1:8122", "test-service-key");
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(new TextEncoder().encode("artifact body"), {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      }),
    );

    const artifact = await getArtifact("run/path/report.txt");

    expect(artifact.artifactId).toBe("run/path/report.txt");
    expect(artifact.mediaType).toBe("text/plain; charset=utf-8");
    expect(artifact.size).toBe(13);
    expect(new TextDecoder().decode(artifact.bytes)).toBe("artifact body");
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8122/v1/artifacts/run%2Fpath%2Freport.txt",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-API-Key": "test-service-key" }),
      }),
    );
  });

  it("fails locally when the console is not connected", async () => {
    await expect(getArtifact("report")).rejects.toMatchObject({ status: 0 });
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("manifest inspection", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setConfig("http://127.0.0.1:8122", "test-service-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  it("loads a safely projected manifest using an encoded reference", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ manifest_ref: "eu://quality", kind: "executable_unit" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(getManifest("eu://quality")).resolves.toMatchObject({
      manifest_ref: "eu://quality",
    });
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8122/v1/manifests/eu%3A%2F%2Fquality",
      expect.anything(),
    );
  });

  it("loads audit-authorized run links separately", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ manifest_ref: "eu://quality", runs: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await listManifestRuns("eu://quality");
    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8122/v1/manifests/eu%3A%2F%2Fquality/runs",
      expect.anything(),
    );
  });
});

describe("ambiguous operation resolution", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setConfig("http://127.0.0.1:8122", "test-service-key");
    vi.stubGlobal("fetch", vi.fn());
  });

  it("posts an operator determination to the encoded deployment operation route", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ operation_key: "operation/key", state: "FAILED" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(resolveAmbiguousOperation("deployment/ref", "operation/key", {
      resolution: "failed",
      reason: "Provider confirms no commit.",
      receipt: { status: "not_found" },
    })).resolves.toEqual({ operation_key: "operation/key", state: "FAILED" });

    expect(fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:8122/v1/deployments/deployment%2Fref/operations/operation%2Fkey/resolve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          resolution: "failed",
          reason: "Provider confirms no commit.",
          receipt: { status: "not_found" },
        }),
      }),
    );
  });
});
