// @vitest-environment jsdom

import type { AnchorHTMLAttributes } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunTimeline: vi.fn(),
  getRunEvidence: vi.fn(),
  cancelRun: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("run=run-1"),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import RunsPage from "./page";

const runningRun = {
  run_id: "run-1",
  thread_id: "thread-1",
  graph_version_ref: "workflow-1@1",
  status: "running",
  current_step: "worker",
  approval_paused_state: null,
  failure_state: null,
  terminal_output: null,
};

let container: HTMLDivElement;
let root: Root;

function button(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
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
  await act(async () => root.render(<RunsPage />));
  await waitFor(() => expect(button("Cancel")).toBeTruthy());
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  api.listRuns.mockResolvedValue({ runs: [runningRun] });
  api.getRun.mockResolvedValue(runningRun);
  api.getRunTimeline.mockResolvedValue({ entries: [] });
  api.getRunEvidence.mockResolvedValue({ run_id: "run-1", records: [] });
  api.cancelRun.mockResolvedValue({ ...runningRun, status: "cancelled" });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("run cancellation", () => {
  it("leaves the run visible and makes no API call when confirmation is declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountPage();

    await act(async () => button("Cancel")?.click());

    expect(confirm).toHaveBeenCalledWith("Cancel run run-1?");
    expect(api.cancelRun).not.toHaveBeenCalled();
    expect(container.textContent).toContain("run-1");
    expect(button("Cancel")).toBeTruthy();
  });

  it("keeps a cancellation API failure visible", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.cancelRun.mockRejectedValue(new Error("control plane offline"));
    await mountPage();

    await act(async () => button("Cancel")?.click());
    await waitFor(() => {
      const alert = container.querySelector('[role="alert"]');
      expect(alert).toBeTruthy();
      expect(alert?.textContent).toContain("Cancel failed: Error: control plane offline");
    });

    expect(button("Cancel")).toBeTruthy();
  });
});
