// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getTenantCost: vi.fn(),
  getEconomicsConfiguration: vi.fn(),
  listDeployments: vi.fn(),
  getUnitEconomics: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));
vi.mock("@/app/lib/config", () => ({
  getTenant: () => "evaluation-studio-v1",
  isConfigured: () => true,
}));

import CostPage from "./page";

describe("workflow economics provenance", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    api.getTenantCost.mockResolvedValue({
      tenant_id: "evaluation-studio-v1",
      period_start: "2026-08-01T00:00:00Z",
      period_end: "2026-09-01T00:00:00Z",
      actual_spend_usd: 0,
      paid_spend_usd: 0,
      estimated_spend_usd: 0,
      active_exposure_usd: 0,
      ambiguous_exposure_usd: 0,
      budget_consumed_usd: 0,
      synthetic_control_usd: 0,
      budget_cap_usd: null,
    });
    api.getEconomicsConfiguration.mockResolvedValue({
      tenant_id: "evaluation-studio-v1",
      deployment_ref: "economics-ui-fixture-v1",
      per_run_cap_usd: null,
      failure_mode: "fail_open",
      source: "service_runtime",
    });
    api.listDeployments.mockResolvedValue([]);
    api.getUnitEconomics.mockResolvedValue({
      window_runs: 2,
      successful_runs: 1,
      failed_runs: 1,
      in_flight_runs: 0,
      success_rate: 0.5,
      total_cost_usd: 0.12,
      terminal_cost_usd: 0.12,
      cost_on_successful_usd: 0.12,
      cost_on_failed_usd: 0,
      cost_on_in_flight_usd: 0,
      cost_per_successful_run_usd: 0.12,
      mean_cost_per_successful_run_usd: 0.12,
      failure_tax_usd: 0,
      failure_tax_ratio: 0,
      estimated_total_cost_usd: 0.08,
      estimated_terminal_cost_usd: 0.08,
      estimated_cost_on_successful_usd: 0,
      estimated_cost_on_failed_usd: 0.08,
      estimated_cost_on_in_flight_usd: 0,
      estimated_cost_per_successful_run_usd: 0.08,
      estimated_mean_cost_per_successful_run_usd: 0,
      estimated_failure_tax_usd: 0.08,
      estimated_failure_tax_ratio: 1,
      runs_with_cost: 1,
      runs_with_estimated_cost: 1,
      by_workflow: [
        {
          workflow_name: "economics-ui-fixture",
          runs: 2,
          successful_runs: 1,
          failed_runs: 1,
          success_rate: 0.5,
          terminal_cost_usd: 0.12,
          cost_per_successful_run_usd: 0.12,
          failure_tax_usd: 0,
          estimated_terminal_cost_usd: 0.08,
          estimated_cost_per_successful_run_usd: 0.08,
          estimated_failure_tax_usd: 0.08,
        },
      ],
      by_tenant: [],
      quality: null,
      note: "Measured and estimated spend are separate.",
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("labels measured and estimated workflow totals separately", async () => {
    await act(async () => root.render(<CostPage />));

    await vi.waitFor(() => {
      expect(host.textContent).toContain("economics-ui-fixture");
      expect(host.textContent).toContain("$0.12 measured");
      expect(host.textContent).toContain("$0.08 estimated");
    });
  });
});
