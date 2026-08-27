// @vitest-environment jsdom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getArtifact: vi.fn(),
  listRuns: vi.fn(),
}));

vi.mock("@/app/lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
    }
  },
  getArtifact: api.getArtifact,
  listRuns: api.listRuns,
}));

vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));

import ArtifactsPage from "./page";

describe("ArtifactsPage", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    api.listRuns.mockResolvedValue({
      runs: [
        {
          run_id: "run-1",
          terminal_output: {
            artifact: {
              key: "run-1/node/report.json",
              content_type: "application/json",
              size: 18,
            },
          },
        },
      ],
      total: 1,
    });
    api.getArtifact.mockResolvedValue({
      artifactId: "run-1/node/report.json",
      bytes: new TextEncoder().encode('{"status":"ready"}'),
      mediaType: "application/json",
      size: 18,
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("discovers run artifact references and previews retrieved content", async () => {
    await act(async () => root.render(<ArtifactsPage />));
    await act(async () => await Promise.resolve());

    const discovered = Array.from(host.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("run-1/node/report.json"),
    );
    expect(discovered).toBeTruthy();
    await act(async () => discovered!.click());

    expect(api.getArtifact).toHaveBeenCalledWith("run-1/node/report.json");
    expect(host.textContent).toContain("application/json");
    expect(host.textContent).toContain('"status": "ready"');
  });

  it("validates a blank direct lookup without issuing a request", async () => {
    await act(async () => root.render(<ArtifactsPage />));
    await act(async () => await Promise.resolve());
    const load = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent === "Load artifact",
    );
    await act(async () => load!.click());

    expect(api.getArtifact).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Enter an artifact ID");
  });

  it("surfaces scoped access failures instead of reporting an empty tenant", async () => {
    api.listRuns.mockRejectedValueOnce(new Error("authentication required"));

    await act(async () => root.render(<ArtifactsPage />));
    await act(async () => await Promise.resolve());

    expect(host.textContent).toContain("Artifact references unavailable");
    expect(host.textContent).toContain("active tenant/workspace");
    expect(host.textContent).toContain("authenticated");
    expect(host.textContent).not.toContain("No artifact references were found");
  });

  it("keeps the empty-tenant state for a successful empty response", async () => {
    api.listRuns.mockResolvedValueOnce({ runs: [], total: 0 });

    await act(async () => root.render(<ArtifactsPage />));
    await act(async () => await Promise.resolve());

    expect(host.textContent).toContain("No artifact references were found");
    expect(host.textContent).not.toContain("Artifact references unavailable");
  });
});
