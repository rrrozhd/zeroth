// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  createTemplate: vi.fn(),
  listTemplates: vi.fn(),
  deleteTemplateVersion: vi.fn(),
  getIdentity: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import { ToastProvider } from "@/app/components/Toast";
import { ApiError } from "@/app/lib/api";

import TemplatesPage from "./page";

let container: HTMLDivElement;
let root: Root;

function deleteButton(): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((button) =>
    button.textContent?.includes("Delete version"),
  );
}

function newButton(): HTMLButtonElement | null {
  return container.querySelector('[data-evidence-id="templates-new"]');
}

function input(evidenceId: string): HTMLInputElement | HTMLTextAreaElement {
  return container.querySelector(`[data-evidence-id="${evidenceId}"]`)!;
}

function evidence(evidenceId: string): HTMLElement {
  return container.querySelector(`[data-evidence-id="${evidenceId}"]`)!;
}

async function setControlValue(
  control: HTMLInputElement | HTMLTextAreaElement,
  value: string,
) {
  await act(async () => {
    const prototype = control instanceof window.HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
  });
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

async function mountPage() {
  await act(async () => {
    root.render(
      <ToastProvider>
        <TemplatesPage />
      </ToastProvider>,
    );
  });
  await waitFor(() => expect(api.listTemplates).toHaveBeenCalledTimes(1));
}

async function mountCreateForm() {
  await mountPage();
  await waitFor(() => expect(newButton()).not.toBeNull());
  await act(async () => newButton()?.click());
  await waitFor(() => expect(input("templates-name")).toBeTruthy());
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
  api.getIdentity.mockResolvedValue({
    subject: "admin-1",
    roles: ["admin"],
    tenant_id: "tenant-a",
    workspace_id: "workspace-a",
  });
  api.createTemplate.mockResolvedValue({
    name: "complex",
    version: 1,
    description: "",
    template_str: "{% for item in items %}{{ item.name }}{% endfor %}",
    variables: ["items"],
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

describe("template evidence selectors", () => {
  it("identifies selected details, the immutable body, and create cancellation", async () => {
    await mountDetail();

    expect(evidence("templates-detail").textContent).toContain("welcome@v2");
    expect(evidence("templates-detail-body").textContent).toContain("Hello {{ name }}");

    await act(async () => newButton()?.click());
    expect(evidence("templates-cancel")).toBeTruthy();
  });
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

  it("explains a dependency conflict in place and keeps the version selected", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.deleteTemplateVersion.mockRejectedValue(
      new ApiError(409, "template version is referenced by workflow incident-response"),
    );
    await mountDetail();

    await act(async () => deleteButton()?.click());
    await waitFor(() => {
      const conflict = container.querySelector('[data-evidence-id="templates-delete-conflict"]');
      expect(conflict?.textContent).toContain("still in use");
      expect(conflict?.textContent).toContain("Remove or repin");
      expect(conflict?.textContent).toContain("incident-response");
    });

    expect(container.textContent).toContain("welcome@v2");
    expect(deleteButton()?.disabled).toBe(false);
  });
});

describe("template mutation access", () => {
  it.each(["operator", "reviewer"])(
    "keeps %s read-only and explains the tenant/workspace capability scope",
    async (role) => {
      api.getIdentity.mockResolvedValue({
        subject: `${role}-1`,
        roles: [role],
        tenant_id: "tenant-a",
        workspace_id: "workspace-a",
      });

      await mountPage();
      await waitFor(() => expect(container.textContent).toContain("tenant-a / workspace-a"));

      expect(container.textContent).toContain(`${role} does not include template:admin`);
      expect(newButton()).toBeNull();
      const template = Array.from(container.querySelectorAll("button")).find((button) =>
        button.textContent?.includes("welcome"),
      );
      await act(async () => template?.click());
      expect(deleteButton()).toBeUndefined();
    },
  );

  it.each(["admin", "platform_admin"])(
    "shows tenant-scoped mutation controls for %s",
    async (role) => {
      api.getIdentity.mockResolvedValue({
        subject: `${role}-1`,
        roles: [role],
        tenant_id: "tenant-b",
        workspace_id: null,
      });

      await mountPage();
      await waitFor(() => expect(newButton()).not.toBeNull());

      expect(container.textContent).toContain("tenant-b / tenant-wide");
      expect(container.textContent).toContain("template:admin");
    },
  );

  it("defers configured custom-role capability decisions to the API", async () => {
    api.getIdentity.mockResolvedValue({
      subject: "template-manager-1",
      roles: ["template_manager"],
      tenant_id: "tenant-c",
      workspace_id: "workspace-c",
    });

    await mountPage();
    await waitFor(() => expect(newButton()).not.toBeNull());

    expect(container.textContent).toContain("API will validate template:admin");
  });

  it("fails closed when identity and scope cannot be verified", async () => {
    api.getIdentity.mockRejectedValue(new Error("identity unavailable"));

    await mountPage();
    await waitFor(() => expect(container.textContent).toContain("create/delete hidden"));

    expect(newButton()).toBeNull();
  });
});

describe("template creation", () => {
  it("exposes required fields, linked errors, and focuses the first invalid field", async () => {
    await mountCreateForm();
    const name = input("templates-name");
    const version = input("templates-version");
    const body = input("templates-body");

    expect(name.required).toBe(true);
    expect(version.required).toBe(true);
    expect(body.required).toBe(true);
    expect(name.getAttribute("aria-required")).toBe("true");
    expect(name.style.outline).not.toBe("none");
    expect(body.style.outline).not.toBe("none");

    await act(async () => {
      container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(name.getAttribute("aria-describedby")).toContain("templates-name-error");
    expect(container.querySelector("#templates-name-error")?.getAttribute("role")).toBe("alert");
    expect(document.activeElement).toBe(name);
    expect(api.createTemplate).not.toHaveBeenCalled();
  });

  it("lets the server extract complex Jinja variables instead of sending a partial client list", async () => {
    await mountCreateForm();
    const complexBody =
      "{% for item in items %}{{ item.name }}{% endfor %} {{ fallback | default(owner.name) }}";
    expect(container.textContent).toContain(
      "server extracts variables from the complete Jinja2 syntax tree",
    );
    await setControlValue(input("templates-name"), "complex");
    await setControlValue(input("templates-body"), complexBody);

    await act(async () => {
      container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });
    await waitFor(() => expect(api.createTemplate).toHaveBeenCalledTimes(1));

    expect(api.createTemplate).toHaveBeenCalledWith({
      name: "complex",
      template_str: complexBody,
      version: 1,
      description: "",
    });
  });

  it.each([
    ["templates-name", "bad name", "Use letters, numbers, dots, underscores, or hyphens", "name"],
    ["templates-name", `a${"b".repeat(128)}`, "128 characters or fewer", "name"],
    ["templates-version", "1.5", "whole number", "version"],
    ["templates-version", "1000001", "whole number", "version"],
    ["templates-description", "d".repeat(2001), "2,000 characters or fewer", "description"],
    ["templates-body", "x".repeat(100001), "100,000 characters or fewer", "body"],
  ])(
    "associates the %s boundary with its field",
    async (evidenceId, value, expectedMessage, expectedField) => {
      await mountCreateForm();
      await setControlValue(input("templates-name"), "valid-name");
      await setControlValue(input("templates-body"), "Hello {{ input.name }}");
      await setControlValue(input(evidenceId), value);

      await act(async () => {
        container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      });

      const control = input(evidenceId);
      expect(control.getAttribute("aria-invalid")).toBe("true");
      expect(control.getAttribute("aria-describedby")).toContain(
        `templates-${expectedField}-error`,
      );
      expect(container.querySelector(`#templates-${expectedField}-error`)?.textContent).toContain(
        expectedMessage,
      );
      expect(document.activeElement).toBe(control);
      expect(api.createTemplate).not.toHaveBeenCalled();
    },
  );

  it.each([
    [422, "invalid Jinja syntax", "templates-body", "Template body is invalid Jinja2"],
    [409, "template version already exists", "templates-name", "already exists"],
  ])(
    "associates API status %s with the actionable field",
    async (status, detail, evidenceId, expectedMessage) => {
      api.createTemplate.mockRejectedValueOnce(new ApiError(status, detail));
      await mountCreateForm();
      await setControlValue(input("templates-name"), "conflict-name");
      await setControlValue(input("templates-body"), "Hello {{ input.name }}");

      await act(async () => {
        container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      });
      await waitFor(() => {
        expect(container.querySelector(`#${evidenceId}-error`)?.textContent).toContain(
          expectedMessage,
        );
      });

      expect(input(evidenceId).getAttribute("aria-invalid")).toBe("true");
      expect(document.activeElement).toBe(input(evidenceId));
    },
  );
});
