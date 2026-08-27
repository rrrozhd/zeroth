// @vitest-environment jsdom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getIdentity: vi.fn(), listDeployments: vi.fn() }));

vi.mock("next/navigation", () => ({ usePathname: () => "/metrics" }));
vi.mock("@/app/lib/api", () => ({
  getIdentity: api.getIdentity,
  listDeployments: api.listDeployments,
}));
vi.mock("@/app/lib/config", () => ({ getEnv: () => "local", getTenant: () => "configured-label" }));

import { Topbar } from "./Topbar";

describe("Topbar authenticated scope", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    api.getIdentity.mockResolvedValue({
      subject: "reviewer-1",
      roles: ["reviewer"],
      tenant_id: "tenant-a",
      workspace_id: "workspace-b",
    });
    api.listDeployments.mockResolvedValue([
      { deployment_ref: "quality-v1", serving: true },
    ]);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("shows the credential's authoritative tenant, workspace, and role", async () => {
    await act(async () =>
      root.render(<Topbar sidebarCollapsed={false} onSidebarToggle={() => undefined} />),
    );
    await act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));

    expect(host.textContent).toContain("tenant-a");
    expect(host.textContent).toContain("workspace-b");
    expect(host.textContent).toContain("reviewer");
    expect(host.querySelector('[aria-label="Scope: tenant-a / workspace-b; roles: reviewer"]'))
      .toBeTruthy();
  });
});
