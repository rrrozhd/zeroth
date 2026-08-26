// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listDeployments: vi.fn(),
  listCertifications: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import { ToastProvider } from "@/app/components/Toast";

import DeploymentsPage from "./page";

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
      await act(async () => {
        await new Promise((resolve) => window.setTimeout(resolve, 0));
      });
    }
  }
  throw failure;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.localStorage.setItem("zeroth.apiKey", "operator-key");
  api.listDeployments.mockResolvedValue([
    {
      deployment_ref: "production/support-agent",
      version: 1,
      graph_version_ref: "support-graph@1",
      status: "active",
      serving: true,
      created_at: "2026-08-26T12:00:00Z",
    },
  ]);
  api.listCertifications.mockResolvedValue([
    {
      certification_id: "a".repeat(32),
      tenant_id: "default",
      workspace_id: null,
      app_name: "support-agent",
      app_commit: "1".repeat(40),
      image_digest: `sha256:${"2".repeat(64)}`,
      state: "test_deployable",
      promotion_target_key: null,
      override: {
        scopes: ["receipt_expired"],
        reason: "approved recovery window",
        actor_id: "admin-1",
        created_at: "2026-08-26T12:00:00Z",
        expires_at: "2026-08-26T12:10:00Z",
      },
      evaluation: {
        certification_id: "a".repeat(32),
        state: "test_deployable",
        test_deployable: true,
        production_ready: false,
        override_active: true,
        blockers: [
          {
            code: "environment_not_certified",
            message: "The receipt does not authorize the production environment.",
            remediation: "Certify the artifact for production.",
            overridable: true,
          },
        ],
      },
      events: [],
      created_at: "2026-08-26T12:00:00Z",
      updated_at: "2026-08-26T12:00:00Z",
    },
  ]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("deployment certification posture", () => {
  it("shows production blockers and their remediation for the selected app", async () => {
    await act(async () => {
      root.render(
        <ToastProvider>
          <DeploymentsPage />
        </ToastProvider>,
      );
    });
    await waitFor(() => {
      const deployment = Array.from(container.querySelectorAll("button")).find((button) =>
        button.textContent?.includes("production/support-agent"),
      );
      expect(deployment).toBeTruthy();
      deployment?.click();
    });
    await waitFor(() => {
      expect(container.textContent).toContain("Production certification");
      expect(container.textContent).toContain("environment_not_certified");
      expect(container.textContent).toContain("Certify the artifact for production.");
      expect(container.textContent).toContain("approved recovery window");
    });
  });
});
