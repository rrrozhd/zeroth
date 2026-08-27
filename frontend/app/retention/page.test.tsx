// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getRetentionPolicy: vi.fn(),
  listErasureHistory: vi.fn(),
  listLegalHolds: vi.fn(),
  placeLegalHold: vi.fn(),
  putRetentionPolicy: vi.fn(),
  releaseLegalHold: vi.fn(),
  requestErasure: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));
vi.mock("@/app/lib/config", () => ({ getTenant: () => "tenant-1", isConfigured: () => true }));

import RetentionPage from "./page";

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

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  api.getRetentionPolicy.mockResolvedValue({
    tenant_id: "tenant-1",
    enabled: true,
    run_ttl_seconds: null,
    audit_ttl_seconds: null,
  });
  api.listLegalHolds.mockResolvedValue([
    {
      hold_id: "hold-persisted",
      tenant_id: "tenant-1",
      run_id: "run-42",
      reason: "litigation",
      placed_by: "admin",
      active: true,
    },
  ]);
  api.listErasureHistory.mockResolvedValue([]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("retention legal holds", () => {
  it("fails closed before the authoritative retention access check resolves", () => {
    const html = renderToString(<RetentionPage />);

    expect(html).not.toContain("Place hold");
    expect(html).not.toContain("Stage erasure request");
  });

  it("reloads active holds from persistent tenant storage", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(container.textContent).toContain("hold-persisted"));

    expect(api.listLegalHolds).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("litigation");
    expect(container.textContent).not.toContain("placed this session");
  });

  it("gives every erasure control a stable evidence identity", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(container.textContent).toContain("Erasure requests"));

    for (const evidenceId of [
      "retention.erasure.card",
      "retention.erasure.scope.run",
      "retention.erasure.scope.tenant",
      "retention.erasure.run-id",
      "retention.erasure.note",
      "retention.erasure.stage",
    ]) {
      expect(container.querySelector(`[data-evidence-id="${evidenceId}"]`)).not.toBeNull();
    }
  });

  it("gives staged erasure actions stable evidence identities", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(container.textContent).toContain("Erasure requests"));

    const runId = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.erasure.run-id"]',
    );
    expect(runId).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(runId, "run-disposable-1");
      runId?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(
        '[data-evidence-id="retention.erasure.stage"]',
      )?.click();
    });

    expect(
      container.querySelector('[data-evidence-id="retention.erasure.item.ER-1"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-evidence-id="retention.erasure.execute.ER-1"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-evidence-id="retention.erasure.discard.ER-1"]'),
    ).not.toBeNull();
  });

  it("restores durable erasure activity after a fresh render", async () => {
    api.listErasureHistory.mockResolvedValue([
      {
        log_id: "history-1",
        run_id: "run-erased",
        action: "erase_run_complete",
        reason: "rte",
        detail: { external_cleanup_status: "complete" },
        created_at: "2026-08-25T12:00:00Z",
      },
    ]);

    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(container.textContent).toContain("erase_run_complete"));

    expect(
      container.querySelector('[data-evidence-id="retention.erasure.history.history-1"]'),
    ).not.toBeNull();
  });

  it("does not expose legal-hold or erasure controls when retention access is denied", async () => {
    api.getRetentionPolicy.mockRejectedValue("403 forbidden");
    api.listLegalHolds.mockRejectedValue("403 forbidden");

    await act(async () => root.render(<RetentionPage />));
    await waitFor(() =>
      expect(container.textContent).toContain(
        "Retention controls are hidden because this API key cannot read",
      ),
    );

    const labels = Array.from(container.querySelectorAll("button")).map((button) =>
      button.textContent?.trim(),
    );
    expect(labels).not.toContain("Place hold");
    expect(labels).not.toContain("Stage erasure request");
    expect(container.querySelector('[data-evidence-id="retention.legal-holds.run-id"]')).toBeNull();
    expect(container.querySelector('[data-evidence-id="retention.legal-holds.reason"]')).toBeNull();
    expect(api.placeLegalHold).not.toHaveBeenCalled();
    expect(api.requestErasure).not.toHaveBeenCalled();
  });
});

describe("retention TTL validation", () => {
  it("rejects zero days before sending a policy the API cannot accept", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(api.getRetentionPolicy).toHaveBeenCalledTimes(1));

    const inputs = Array.from(container.querySelectorAll<HTMLInputElement>('input[inputmode="decimal"]'));
    expect(inputs).toHaveLength(2);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(inputs[0], "0");
      inputs[0].dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(container.textContent).toContain(
      "TTL must resolve to 1–2147483647 seconds (max 24855.1348 days",
    );
    expect(inputs[0].getAttribute("aria-invalid")).toBe("true");
    expect(inputs[0].getAttribute("aria-describedby")).toBeTruthy();
    const save = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Save policy"),
    );
    expect(save?.disabled).toBe(true);
    expect(api.putRetentionPolicy).not.toHaveBeenCalled();
  });

  it("rejects values above the portable PostgreSQL INTEGER maximum", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(api.getRetentionPolicy).toHaveBeenCalledTimes(1));

    const input = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.policy.run-payloads-ttl"]',
    );
    expect(input).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, "24855.1349");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(container.textContent).toContain(
      "TTL must resolve to 1–2147483647 seconds (max 24855.1348 days",
    );
    expect(input?.getAttribute("aria-invalid")).toBe("true");
    expect(api.putRetentionPolicy).not.toHaveBeenCalled();
  });

  it("accepts the exact portable PostgreSQL INTEGER maximum", async () => {
    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(api.getRetentionPolicy).toHaveBeenCalledTimes(1));

    const input = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.policy.run-payloads-ttl"]',
    );
    expect(input).not.toBeNull();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, "24855.1348");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(input?.getAttribute("aria-invalid")).toBe("false");
    expect(container.textContent).toContain("24855d 3h 14m 7s");
  });

  it("renders the persisted maximum compactly without losing its exact second value", async () => {
    api.getRetentionPolicy.mockResolvedValue({
      tenant_id: "tenant-1",
      enabled: true,
      run_ttl_seconds: 2_147_483_647,
      audit_ttl_seconds: null,
    });

    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(api.getRetentionPolicy).toHaveBeenCalledTimes(1));

    const input = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.policy.run-payloads-ttl"]',
    );
    expect(input?.value).toBe("24855.1348");
    expect(input?.getAttribute("aria-invalid")).toBe("false");
    expect(container.textContent).toContain("24855d 3h 14m 7s");
  });

  it("round-trips a persisted one-second TTL without rendering it as zero", async () => {
    api.getRetentionPolicy.mockResolvedValue({
      tenant_id: "tenant-1",
      enabled: true,
      run_ttl_seconds: 1,
      audit_ttl_seconds: null,
    });
    api.putRetentionPolicy.mockResolvedValue({
      tenant_id: "tenant-1",
      enabled: true,
      run_ttl_seconds: 1,
      audit_ttl_seconds: null,
    });

    await act(async () => root.render(<RetentionPage />));
    await waitFor(() => expect(api.getRetentionPolicy).toHaveBeenCalledTimes(1));

    const input = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.policy.run-payloads-ttl"]',
    );
    expect(input?.value).toBe("0.0000115741");
    expect(input?.getAttribute("aria-invalid")).toBe("false");

    const auditInput = container.querySelector<HTMLInputElement>(
      '[data-evidence-id="retention.policy.audit-records-ttl"]',
    );
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(auditInput, "1");
      auditInput?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      Array.from(container.querySelectorAll("button"))
        .find((button) => button.textContent?.includes("Save policy"))
        ?.click();
    });

    expect(api.putRetentionPolicy).toHaveBeenCalledWith({
      enabled: true,
      run_ttl_seconds: 1,
      audit_ttl_seconds: 86_400,
    });
  });
});
