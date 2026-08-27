// @vitest-environment jsdom

import type { AnchorHTMLAttributes } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createWorkflow: vi.fn(),
  push: vi.fn(),
  reload: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@/app/hooks/useLoad", () => ({
  useLoad: () => ({
    data: [
      { id: "workflow-1", name: "Incident review", status: "draft", version: 1 },
      { id: "workflow-2", name: "Incident review", status: "published", version: 2 },
    ],
    error: null,
    loading: false,
    reload: mocks.reload,
  }),
}));

vi.mock("@/app/components/Toast", () => ({
  useToast: () => mocks.toast,
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  createWorkflow: mocks.createWorkflow,
}));

import { assignEvidenceIdentities } from "@/app/lib/evidence-identity";
import StudioPage from "./page";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mocks.createWorkflow.mockReset();
  mocks.push.mockReset();
  mocks.reload.mockReset();
  mocks.toast.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("Studio overview", () => {
  it("presents workflows and templates as operational lists", async () => {
    await act(async () => root.render(<StudioPage />));

    const lists = Array.from(container.querySelectorAll('[role="list"]'));
    expect(lists.map((list) => list.getAttribute("aria-label"))).toEqual([
      "Workflows",
      "Workflow templates",
    ]);
    expect(container.textContent).toContain("Starts with an empty canvas");
    expect(container.textContent).toContain("Create blank workflow");
  });

  it("scopes repeated workflow-row controls by stable workflow identity", async () => {
    await act(async () => root.render(<StudioPage />));

    const result = assignEvidenceIdentities(container, "/console/studio/");

    expect(result.errors).toEqual([]);
    expect(
      Array.from(container.querySelectorAll('a[href^="/studio/edit"]'), (link) =>
        link.getAttribute("data-evidence-id"),
      ),
    ).toEqual(expect.arrayContaining([
      expect.stringContaining("workflow-workflow-1"),
      expect.stringContaining("workflow-workflow-2"),
    ]));
  });

  it("creates a named blank workflow and opens its empty editor", async () => {
    mocks.createWorkflow.mockResolvedValue({ id: "workflow-new" });
    await act(async () => root.render(<StudioPage />));

    const input = container.querySelector<HTMLInputElement>("#new-workflow-name");
    const create = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Create",
    );
    expect(input).toBeTruthy();
    expect(create).toBeTruthy();

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        input,
        "Blank safety flow",
      );
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => create?.click());

    expect(mocks.createWorkflow).toHaveBeenCalledWith("Blank safety flow");
    expect(mocks.push).toHaveBeenCalledWith("/studio/edit?id=workflow-new");
  });
});
