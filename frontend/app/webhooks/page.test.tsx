// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listWebhookSubscriptions: vi.fn(),
  listWebhookDeadLetters: vi.fn(),
  deleteWebhookSubscription: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import WebhooksPage from "./page";

let container: HTMLDivElement;
let root: Root;

function deactivateButton(): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (button) => button.textContent?.trim() === "Deactivate",
  );
}

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

async function mountPage() {
  await act(async () => root.render(<WebhooksPage />));
  await waitFor(() => expect(deactivateButton()).toBeTruthy());
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.localStorage.setItem("zeroth.apiKey", "operator-key");
  api.listWebhookSubscriptions.mockResolvedValue({
    subscriptions: [
      {
        subscription_id: "sub-1",
        target_url: "https://example.test/hooks",
        event_types: ["run.completed"],
        active: true,
        created_at: "2026-08-11T12:00:00Z",
      },
    ],
  });
  api.listWebhookDeadLetters.mockResolvedValue({ dead_letters: [] });
  api.deleteWebhookSubscription.mockResolvedValue(undefined);
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

describe("webhook deactivation", () => {
  it("keeps the mounted subscription visible when confirmation is declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountPage();

    await act(async () => deactivateButton()?.click());

    expect(confirm).toHaveBeenCalledWith(
      "Deactivate webhook subscription sub-1 (https://example.test/hooks)?",
    );
    expect(api.deleteWebhookSubscription).not.toHaveBeenCalled();
    expect(container.textContent).toContain("https://example.test/hooks");
    expect(deactivateButton()).toBeTruthy();
  });

  it("shows a mounted API failure and keeps the subscription", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteWebhookSubscription.mockRejectedValue(new Error("delivery store offline"));
    await mountPage();

    await act(async () => deactivateButton()?.click());
    await waitFor(() =>
      expect(container.textContent).toContain(
        "Deactivation failed: Error: delivery store offline",
      ),
    );

    expect(api.deleteWebhookSubscription).toHaveBeenCalledWith("sub-1");
    expect(container.textContent).toContain("https://example.test/hooks");
    expect(deactivateButton()).toBeTruthy();
  });
});
