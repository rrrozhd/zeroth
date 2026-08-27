// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getIdentity: vi.fn() }));
const regulus = vi.hoisted(() => ({
  rgCapabilities: vi.fn(),
  rgEnforcementActions: vi.fn(),
  rgPolicyActions: vi.fn(),
  rgApproveAction: vi.fn(),
  rgRejectAction: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));
vi.mock("@/app/lib/regulusApi", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/regulusApi")>("@/app/lib/regulusApi")),
  ...regulus,
}));
vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));

import { ToastProvider } from "@/app/components/Toast";
import CapabilitiesPage from "./capabilities/page";
import EnforcementPage from "./enforcement/page";

let container: HTMLDivElement;
let root: Root;

async function waitFor(assertion: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      failure = error;
      await act(async () => new Promise((resolve) => window.setTimeout(resolve, 0)));
    }
  }
  throw failure;
}

async function render(page: React.ReactNode) {
  await act(async () => root.render(<ToastProvider>{page}</ToastProvider>));
  await waitFor(() => expect(api.getIdentity).toHaveBeenCalledTimes(1));
}

function setRole(role: string, tenant = "tenant-a", workspace: string | null = "workspace-a") {
  api.getIdentity.mockResolvedValue({
    subject: `${role}-subject`,
    roles: [role],
    tenant_id: tenant,
    workspace_id: workspace,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setRole("admin");
  regulus.rgCapabilities.mockResolvedValue([]);
  regulus.rgEnforcementActions.mockResolvedValue([
    {
      id: 12,
      capability_id: "capability-a",
      action_type: "pin_model",
      status: "pending",
      reason: "Measured regression",
      before_config: {},
      after_config: { model: "candidate" },
      created_at: "2026-08-25T12:00:00Z",
      approved_at: null,
      approver_sub: null,
    },
  ]);
  regulus.rgPolicyActions.mockResolvedValue([]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("Capabilities authorization states", () => {
  it.each(["operator", "reviewer"])(
    "shows an inspectable %s denial without issuing protected reads",
    async (role) => {
      setRole(role);
      await render(<CapabilitiesPage />);
      await waitFor(() => {
        expect(container.querySelector('[data-evidence-id="regulus.capabilities.access.restricted"]'))
          .not.toBeNull();
      });

      expect(container.textContent).toContain("tenant-a / workspace-a");
      expect(container.textContent).toContain(role);
      expect(container.textContent).toContain("metrics:read");
      expect(regulus.rgCapabilities).not.toHaveBeenCalled();
    },
  );

  it("loads the stable registry for an admin and exposes authoritative scope", async () => {
    setRole("admin", "tenant-b", null);
    regulus.rgCapabilities.mockResolvedValue([
      {
        id: "colliding-capability",
        tenant_id: "tenant-b",
        name: "Tenant B capability",
        type: "COST",
        description: "Isolated fixture",
        criticality: "LOW",
        is_protected: false,
        valuation_config: {},
        created_at: "2026-08-25T12:00:00Z",
      },
    ]);
    await render(<CapabilitiesPage />);
    await waitFor(() => expect(regulus.rgCapabilities).toHaveBeenCalledTimes(1));

    expect(container.querySelector('[data-evidence-id="regulus.capabilities.scope"]')?.textContent)
      .toContain("tenant-b / tenant-wide");
    expect(container.querySelector('[data-evidence-id="regulus.capabilities.registry"]'))
      .not.toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.capabilities.capability.colliding-capability"]'))
      .not.toBeNull();
    expect(container.textContent).toContain("Tenant B capability");
  });
});

describe("Enforcement authorization states", () => {
  it.each(["operator", "reviewer"])(
    "shows an inspectable %s denial without loading protected records",
    async (role) => {
      setRole(role);
      await render(<EnforcementPage />);
      await waitFor(() => {
        expect(container.querySelector('[data-evidence-id="regulus.enforcement.access.restricted"]'))
          .not.toBeNull();
      });

      expect(container.textContent).toContain("metrics:read");
      expect(regulus.rgEnforcementActions).not.toHaveBeenCalled();
      expect(regulus.rgPolicyActions).not.toHaveBeenCalled();
    },
  );

  it("keeps tenant admins read-only while preserving enforcement inspection", async () => {
    setRole("admin");
    await render(<EnforcementPage />);
    await waitFor(() => expect(regulus.rgEnforcementActions).toHaveBeenCalledTimes(1));

    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12"]'))
      .not.toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12.approve"]'))
      .toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12.reject"]'))
      .toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.mutation.restricted"]')?.textContent)
      .toContain("econ:admin");
  });

  it("shows catalogable decision controls only to platform admins", async () => {
    setRole("platform_admin", "tenant-b", null);
    await render(<EnforcementPage />);
    await waitFor(() => expect(regulus.rgEnforcementActions).toHaveBeenCalledTimes(1));

    expect(container.querySelector('[data-evidence-id="regulus.enforcement.scope"]')?.textContent)
      .toContain("tenant-b / tenant-wide");
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12.reason"]'))
      .not.toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12.approve"]'))
      .not.toBeNull();
    expect(container.querySelector('[data-evidence-id="regulus.enforcement.action.12.reject"]'))
      .not.toBeNull();
  });
});
