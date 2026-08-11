// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listTemplates: vi.fn(),
  deleteTemplateVersion: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import { ToastProvider } from "@/app/components/Toast";

import TemplatesPage from "./page";

let container: HTMLDivElement;
let root: Root;

function deleteButton(): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((button) =>
    button.textContent?.includes("Delete version"),
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

async function mountDetail() {
  await act(async () => {
    root.render(
      <ToastProvider>
        <TemplatesPage />
      </ToastProvider>,
    );
  });
  await waitFor(() => {
    const template = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("welcome"),
    );
    expect(template).toBeTruthy();
    template?.click();
  });
  await waitFor(() => expect(deleteButton()).toBeTruthy());
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.localStorage.setItem("zeroth.apiKey", "operator-key");
  api.listTemplates.mockResolvedValue({
    templates: [
      {
        name: "welcome",
        version: 2,
        description: "Greeting",
        template_str: "Hello {{ name }}",
        variables: ["name"],
      },
    ],
  });
  api.deleteTemplateVersion.mockResolvedValue(undefined);
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

describe("template deletion", () => {
  it("keeps the mounted template visible when confirmation is declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountDetail();

    await act(async () => deleteButton()?.click());

    expect(confirm).toHaveBeenCalledWith("Delete welcome@v2? This cannot be undone.");
    expect(api.deleteTemplateVersion).not.toHaveBeenCalled();
    expect(container.textContent).toContain("welcome@v2");
    expect(deleteButton()).toBeTruthy();
  });

  it("shows a mounted API failure and keeps the template", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteTemplateVersion.mockRejectedValue(new Error("registry offline"));
    await mountDetail();

    await act(async () => deleteButton()?.click());
    await waitFor(() =>
      expect(container.textContent).toContain("Delete failed: Error: registry offline"),
    );

    expect(api.deleteTemplateVersion).toHaveBeenCalledWith("welcome", "2");
    expect(deleteButton()).toBeTruthy();
  });
});
