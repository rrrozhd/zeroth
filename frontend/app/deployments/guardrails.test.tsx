// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getDeploymentGuardrails: vi.fn(),
  updateDeploymentGuardrails: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import { DeploymentGuardrailsPanel, GuardrailsPanel } from "./GuardrailsPanel";

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

function input(label: string): HTMLInputElement {
  const element = container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
  if (!element) throw new Error(`missing input ${label}`);
  return element;
}

function setInput(label: string, value: string) {
  const element = input(label);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  api.getDeploymentGuardrails.mockResolvedValue({
    tenant_revision: null,
    deployment_revision: null,
    tenant_overrides: null,
    deployment_overrides: null,
    effective: {
      rate_limit_capacity: 10,
      rate_limit_refill_rate: 1,
      rate_limit_burst: 0,
      quota_daily_limit: null,
      backpressure_queue_depth: 100,
      max_concurrency: 8,
    },
  });
  api.updateDeploymentGuardrails.mockResolvedValue({
    tenant_revision: null,
    deployment_revision: {
      revision_id: "revision-1",
      scope: "deployment",
      deployment_ref: "support-prod",
      policy: { max_concurrency: 4, backpressure_queue_depth: 25 },
      changed_by: "admin-1",
      created_at: "2026-08-14T08:00:00Z",
    },
    tenant_overrides: null,
    deployment_overrides: { max_concurrency: 4, backpressure_queue_depth: 25 },
    effective: {
      rate_limit_capacity: 10,
      rate_limit_refill_rate: 1,
      rate_limit_burst: 0,
      quota_daily_limit: null,
      backpressure_queue_depth: 25,
      max_concurrency: 4,
    },
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("deployment guardrail controls", () => {
  it("does not load controls for a registered non-serving deployment", async () => {
    await act(async () =>
      root.render(<DeploymentGuardrailsPanel refId="peer-prod" serving={false} />),
    );

    expect(container.textContent).toContain("managed by the serving deployment");
    expect(api.getDeploymentGuardrails).not.toHaveBeenCalled();
  });

  it("shows effective settings and appends only explicit deployment overrides", async () => {
    await act(async () => root.render(<GuardrailsPanel refId="support-prod" />));
    await waitFor(() => expect(container.textContent).toContain("Effective settings"));
    expect(container.textContent).toContain("100 queued");
    expect(container.textContent).toContain("8 concurrent");

    await act(async () => {
      setInput("Queue depth override", "25");
      setInput("Concurrency override", "4");
    });
    const save = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Save overrides",
    );
    await act(async () => save?.click());
    await waitFor(() => expect(api.updateDeploymentGuardrails).toHaveBeenCalledTimes(1));

    expect(api.updateDeploymentGuardrails).toHaveBeenCalledWith("support-prod", {
      backpressure_queue_depth: 25,
      max_concurrency: 4,
    });
    expect(container.textContent).toContain("Saved immutable revision");
  });

  it("keeps inputs mounted and gives remediation when saving fails", async () => {
    api.updateDeploymentGuardrails.mockRejectedValue(new Error("permission denied"));
    await act(async () => root.render(<GuardrailsPanel refId="support-prod" />));
    await waitFor(() => expect(container.textContent).toContain("Effective settings"));
    await act(async () => {
      setInput("Concurrency override", "3");
    });
    const save = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Save overrides",
    );
    await act(async () => save?.click());
    await waitFor(() => expect(container.textContent).toContain("permission denied"));

    expect(container.textContent).toContain("Check deployment-admin permissions");
    expect(input("Concurrency override").value).toBe("3");
  });

  it("rejects refill rates slower than one token per day", async () => {
    await act(async () => root.render(<GuardrailsPanel refId="support-prod" />));
    await waitFor(() => expect(container.textContent).toContain("Effective settings"));
    await act(async () => {
      setInput("Refill override", "5e-324");
    });
    const save = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Save overrides",
    );
    await act(async () => save?.click());

    await waitFor(() => expect(container.textContent).toContain("at least 1 token/day"));
    expect(api.updateDeploymentGuardrails).not.toHaveBeenCalled();
  });

  it("shows composed overrides and distinguishes preserve, inherit, and explicit values", async () => {
    api.getDeploymentGuardrails.mockResolvedValue({
      tenant_revision: null,
      deployment_revision: {
        revision_id: "revision-2",
        scope: "deployment",
        deployment_ref: "support-prod",
        policy: { backpressure_queue_depth: 25 },
        changed_by: "admin-1",
        created_at: "2026-08-14T08:01:00Z",
      },
      tenant_overrides: null,
      deployment_overrides: { max_concurrency: 4, backpressure_queue_depth: 25 },
      effective: {
        rate_limit_capacity: 10,
        rate_limit_refill_rate: 1,
        rate_limit_burst: 0,
        quota_daily_limit: 500,
        backpressure_queue_depth: 25,
        max_concurrency: 4,
      },
    });

    await act(async () => root.render(<GuardrailsPanel refId="support-prod" />));
    await waitFor(() => expect(container.textContent).toContain("Effective settings"));
    expect(input("Queue depth override").closest("label")?.textContent).toContain(
      "Active override: 25",
    );
    expect(input("Concurrency override").closest("label")?.textContent).toContain(
      "Active override: 4",
    );
    expect(input("Queue depth override").value).toBe("");

    await act(async () => {
      setInput("Capacity override", "12");
      setInput("Daily quota override", "unlimited");
      setInput("Concurrency override", "inherit");
    });
    const save = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Save overrides",
    );
    await act(async () => save?.click());
    await waitFor(() => expect(api.updateDeploymentGuardrails).toHaveBeenCalledTimes(1));

    expect(api.updateDeploymentGuardrails).toHaveBeenCalledWith("support-prod", {
      rate_limit_capacity: 12,
      quota_daily_limit: null,
      reset_fields: ["max_concurrency"],
    });
  });
});
