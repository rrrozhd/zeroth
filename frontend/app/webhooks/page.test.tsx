// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  createWebhookSubscription: vi.fn(),
  deleteWebhookSubscription: vi.fn(),
  getIdentity: vi.fn(),
  listWebhookDeadLetters: vi.fn(),
  listWebhookDeliveries: vi.fn(),
  listWebhookSubscriptions: vi.fn(),
  replayWebhookDeadLetter: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));
vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));

import WebhooksPage from "./page";
import { WEBHOOK_EVENT_TYPES, webhookFailureText } from "./webhook-ui";
import { ApiError } from "@/app/lib/api";

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
  api.getIdentity.mockResolvedValue({
    subject: "admin-1",
    roles: ["admin"],
    tenant_id: "tenant-a",
    workspace_id: null,
  });
  api.listWebhookSubscriptions.mockResolvedValue({ subscriptions: [], total: 0 });
  api.listWebhookDeadLetters.mockResolvedValue({ dead_letters: [], total: 0 });
  api.listWebhookDeliveries.mockResolvedValue({ deliveries: [], total: 0 });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("webhook operator controls", () => {
  it.each(["operator", "reviewer"])(
    "explains %s access without rendering or loading admin controls",
    async (role) => {
      api.getIdentity.mockResolvedValue({
        subject: `${role}-1`,
        roles: [role],
        tenant_id: "tenant-a",
        workspace_id: "workspace-a",
      });
      api.listWebhookSubscriptions.mockRejectedValue(new ApiError(403, "forbidden"));

      await act(async () => root.render(<WebhooksPage />));
      await waitFor(() => expect(api.getIdentity).toHaveBeenCalledTimes(1));
      await waitFor(() => {
        expect(container.textContent).toContain("Access restricted");
        expect(container.textContent).toContain("admin or platform admin role");
        expect(container.textContent).toContain("webhook:admin capability");
        expect(container.textContent).toContain("tenant-a / workspace-a");
      });

      expect(container.querySelector('[data-evidence-id="webhooks.create"]')).toBeNull();
      expect(container.querySelector('[data-evidence-id="webhooks.dead-letters.refresh"]')).toBeNull();
      expect(api.listWebhookSubscriptions).toHaveBeenCalledTimes(1);
      expect(api.listWebhookDeliveries).not.toHaveBeenCalled();
      expect(api.listWebhookDeadLetters).not.toHaveBeenCalled();
    },
  );

  it("uses the backend capability instead of denying a configured custom role by name", async () => {
    api.getIdentity.mockResolvedValue({
      subject: "webhook-manager-1",
      roles: ["webhook_manager"],
      tenant_id: "tenant-a",
      workspace_id: null,
    });

    await act(async () => root.render(<WebhooksPage />));
    await waitFor(() => {
      expect(container.querySelector('[data-evidence-id="webhooks.create"]')).not.toBeNull();
    });

    expect(container.textContent).toContain("webhook_manager");
    expect(container.querySelector('[data-evidence-id="webhooks.access.restricted"]')).toBeNull();
  });

  it.each(["admin", "platform_admin"])(
    "shows the authoritative tenant scope and admin controls for %s",
    async (role) => {
      api.getIdentity.mockResolvedValue({
        subject: `${role}-1`,
        roles: [role],
        tenant_id: "tenant-b",
        workspace_id: null,
      });

      await act(async () => root.render(<WebhooksPage />));
      await waitFor(() => expect(api.listWebhookSubscriptions).toHaveBeenCalledTimes(1));

      expect(container.textContent).toContain("tenant-b / tenant-wide");
      expect(container.textContent).toContain(role);
      expect(container.querySelector('[data-evidence-id="webhooks.create"]')).not.toBeNull();
    },
  );

  it("does not repeat an HTTP status when the error contains the same value", () => {
    expect(webhookFailureText(503, "HTTP 503")).toBe("HTTP 503");
    expect(webhookFailureText(503, "destination unavailable")).toBe(
      "HTTP 503 · destination unavailable",
    );
    expect(webhookFailureText(null, "timeout")).toBe("timeout");
  });

  it("renders every supported event as a catalogable checkbox", async () => {
    await act(async () => root.render(<WebhooksPage />));
    await waitFor(() => expect(api.listWebhookSubscriptions).toHaveBeenCalledTimes(1));

    const checkboxes = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
    expect(checkboxes).toHaveLength(WEBHOOK_EVENT_TYPES.length);
    for (const eventType of WEBHOOK_EVENT_TYPES) {
      expect(container.querySelector(`[data-evidence-id="webhooks.event.${eventType}"]`)).not.toBeNull();
    }
    expect(container.querySelector('[data-evidence-id="webhooks.target-url"]')).not.toBeNull();
    expect(container.querySelector('[data-evidence-id="webhooks.create"]')).not.toBeNull();
  });

  it("keeps the one-time signing secret out of rendered text until explicitly revealed", async () => {
    api.createWebhookSubscription.mockResolvedValue({
      subscription_id: "sub-1",
      deployment_ref: "deploy-1",
      tenant_id: "tenant-1",
      target_url: "https://example.com/hooks/zeroth",
      secret: "top-secret-signing-value",
      event_types: ["run.completed"],
      active: true,
      created_at: "2026-08-24T12:00:00Z",
      updated_at: "2026-08-24T12:00:00Z",
    });
    await act(async () => root.render(<WebhooksPage />));
    await waitFor(() => expect(api.listWebhookSubscriptions).toHaveBeenCalledTimes(1));

    const target = container.querySelector<HTMLInputElement>('[data-evidence-id="webhooks.target-url"]')!;
    const completed = container.querySelector<HTMLInputElement>('[data-evidence-id="webhooks.event.run.completed"]')!;
    const create = container.querySelector<HTMLButtonElement>('[data-evidence-id="webhooks.create"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
      setter?.call(target, "https://example.com/hooks/zeroth");
      target.dispatchEvent(new Event("input", { bubbles: true }));
      completed.click();
      create.click();
    });
    await waitFor(() => expect(api.createWebhookSubscription).toHaveBeenCalledTimes(1));

    expect(container.textContent).not.toContain("top-secret-signing-value");
    const reveal = container.querySelector<HTMLButtonElement>('[data-evidence-id="webhooks.secret.reveal"]')!;
    expect(reveal).not.toBeNull();
    await act(async () => reveal.click());
    expect(container.textContent).toContain("top-secret-signing-value");
  });

  it("renders durable run and approval correlation without exposing the event payload", async () => {
    api.listWebhookDeliveries.mockResolvedValue({
      total: 1,
      deliveries: [
        {
          delivery_id: "delivery-1",
          subscription_id: "sub-1",
          event_type: "approval.requested",
          event_id: "event-1",
          run_id: "run-1",
          approval_id: "approval-1",
          status: "delivered",
          attempt_count: 1,
          max_attempts: 5,
          last_error: null,
          last_status_code: 204,
          created_at: "2026-08-25T12:00:00Z",
          updated_at: "2026-08-25T12:00:01Z",
        },
      ],
    });
    api.listWebhookDeadLetters.mockResolvedValue({
      total: 1,
      dead_letters: [
        {
          dead_letter_id: "dead-1",
          delivery_id: "delivery-2",
          subscription_id: "sub-1",
          event_type: "approval.resolved",
          event_id: "event-2",
          run_id: "run-2",
          approval_id: "approval-2",
          attempt_count: 5,
          last_error: "timeout",
          last_status_code: null,
          created_at: "2026-08-25T12:00:00Z",
          dead_lettered_at: "2026-08-25T12:00:10Z",
        },
      ],
    });

    await act(async () => root.render(<WebhooksPage />));
    await waitFor(() => expect(api.listWebhookDeliveries).toHaveBeenCalledTimes(1));

    expect(container.textContent).toContain("run run-1");
    expect(container.textContent).toContain("approval approval-1");
    expect(container.textContent).toContain("run run-2");
    expect(container.textContent).toContain("approval approval-2");
    expect(container.textContent).not.toContain("payload_json");
    expect(
      container.querySelector('[data-evidence-id="webhooks.delivery.delivery-1.correlation"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-evidence-id="webhooks.dead-letter.dead-1.correlation"]'),
    ).not.toBeNull();
  });

  it("shows replay failures instead of silently refreshing", async () => {
    api.listWebhookDeadLetters.mockResolvedValue({
      total: 1,
      dead_letters: [
        {
          dead_letter_id: "dead-1",
          delivery_id: "delivery-1",
          subscription_id: "sub-1",
          event_type: "run.failed",
          event_id: "event-1",
          run_id: "run-1",
          approval_id: null,
          attempt_count: 5,
          last_error: "timeout",
          last_status_code: null,
          created_at: "2026-08-25T12:00:00Z",
          dead_lettered_at: "2026-08-25T12:00:10Z",
        },
      ],
    });
    api.replayWebhookDeadLetter.mockRejectedValue(new Error("replay audit unavailable"));
    await act(async () => root.render(<WebhooksPage />));
    await waitFor(() => expect(api.listWebhookDeadLetters).toHaveBeenCalledTimes(1));

    const replay = container.querySelector<HTMLButtonElement>(
      '[data-evidence-id="webhooks.dead-letter.dead-1.replay"]',
    )!;
    await act(async () => replay.click());
    await waitFor(() => {
      expect(container.textContent).toContain(
        "Replay failed: replay audit unavailable",
      );
    });
  });
});
