// @vitest-environment jsdom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getManifest: vi.fn(),
  listManifestRuns: vi.fn(),
}));

vi.mock("@/app/lib/api", () => ({
  getManifest: api.getManifest,
  listManifestRuns: api.listManifestRuns,
  errMsg: (error: { status?: number; message?: string }) =>
    `${error.status ?? ""} ${error.message ?? String(error)}`.trim(),
}));

import { ManifestInspector } from "./ManifestInspector";

describe("ManifestInspector", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    api.getManifest.mockResolvedValue({
      manifest_ref: "eu://quality",
      kind: "executable_unit",
      runtime: "python",
      version: 3,
      onboarding_mode: "project",
      artifact_source_kind: "project_archive",
      entrypoint_type: "project",
      input_mode: "json_stdin",
      output_mode: "json_stdout",
      input_contract_ref: "contract://quality-input",
      output_contract_ref: "contract://quality-output",
      capability_requests: ["network:none"],
      resource_limits: { memory_mb: 256, network_access: false },
      timeout_seconds: 30,
      execution_placement: "local_only",
      side_effect: false,
      content_hash: "abc123",
      input_schema: { type: "object" },
      output_schema: { type: "object" },
    });
    api.listManifestRuns.mockResolvedValue({
      manifest_ref: "eu://quality",
      runs: [{ run_id: "run-linked", node_id: "repair", status: "completed" }],
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("shows safe configuration, immutable hash, contracts, and linked runs", async () => {
    await act(async () => root.render(<ManifestInspector manifestRef="eu://quality" />));
    await act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));

    expect(host.textContent).toContain("project_archive");
    expect(host.textContent).toContain("abc123");
    expect(host.textContent).toContain("contract://quality-input");
    expect(host.querySelector("a")?.getAttribute("href")).toBe("/runs?run=run-linked");
    expect(host.textContent).toContain("Commands, source, environment, and secret bindings stay hidden");
  });

  it("explains when the active role cannot read audit-linked runs", async () => {
    api.listManifestRuns.mockRejectedValue({ status: 403, message: "forbidden" });

    await act(async () => root.render(<ManifestInspector manifestRef="eu://quality" />));
    await act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));

    expect(host.textContent).toContain("Run linkage is hidden");
    expect(host.textContent).toContain("audit evidence");
  });
});
