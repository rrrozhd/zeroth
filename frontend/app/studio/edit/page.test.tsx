// @vitest-environment jsdom

import type { AnchorHTMLAttributes, ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getWorkflow: vi.fn(),
  listNodeTypes: vi.fn(),
  listConnectors: vi.fn(),
  listManifests: vi.fn(),
  listContracts: vi.fn(),
  listWorkflows: vi.fn(),
  listRuns: vi.fn(),
  getHealth: vi.fn(),
  getRun: vi.fn(),
  getRunTimeline: vi.fn(),
  updateWorkflow: vi.fn(),
  publishWorkflow: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("id=workflow-1"),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@xyflow/react", async () => {
  const actual = await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  type SelectionChange = { id: string; type: "select"; selected: boolean };
  type FlowProps = {
    nodes: { id: string; selected?: boolean }[];
    edges: { id: string; selected?: boolean }[];
    onNodesChange: (changes: SelectionChange[]) => void;
    onEdgesChange: (changes: SelectionChange[]) => void;
    onNodeDoubleClick: (event: MouseEvent, node: { id: string }) => void;
    children: ReactNode;
  };

  return {
    ...actual,
    ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
    ReactFlow: ({
      nodes,
      edges,
      onNodesChange,
      onEdgesChange,
      onNodeDoubleClick,
      children,
    }: FlowProps) => (
      <div aria-label="Workflow graph editor">
        {nodes.map((node) => (
          <button
            key={node.id}
            aria-pressed={node.selected ?? false}
            onClick={() => onNodesChange([{ id: node.id, type: "select", selected: true }])}
            onDoubleClick={(event) => onNodeDoubleClick(event.nativeEvent, node)}
          >
            node:{node.id}
          </button>
        ))}
        {edges.map((edge) => (
          <button
            key={edge.id}
            aria-pressed={edge.selected ?? false}
            onClick={() => onEdgesChange([{ id: edge.id, type: "select", selected: true }])}
          >
            edge:{edge.id}
          </button>
        ))}
        {children}
      </div>
    ),
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    Panel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  };
});

import StudioEditPage from "./page";

const workflow = {
  id: "workflow-1",
  name: "Safety workflow",
  status: "draft",
  version: 1,
  entry_step: "worker",
  nodes: [
    {
      id: "worker",
      type: "agent",
      position: { x: 0, y: 0 },
      data: { label: "Worker", config: {} },
    },
    {
      id: "reviewer",
      type: "agent",
      position: { x: 200, y: 0 },
      data: { label: "Reviewer", config: {} },
    },
  ],
  edges: [{ id: "handoff", source: "worker", target: "reviewer", kind: "data" }],
};

const mixedSelectionWorkflow = {
  ...workflow,
  nodes: [
    ...workflow.nodes,
    {
      id: "auditor",
      type: "agent",
      position: { x: 400, y: 0 },
      data: { label: "Auditor", config: {} },
    },
  ],
  edges: [
    ...workflow.edges,
    { id: "audit", source: "reviewer", target: "auditor", kind: "data" },
  ],
};

let container: HTMLDivElement;
let root: Root;

async function mountEditor() {
  await act(async () => {
    root.render(<StudioEditPage />);
  });
  await waitFor(() => expect(button("node:worker")).toBeTruthy());
}

function button(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
}

function buttonContaining(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label),
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

async function changeWorkflowName(value: string) {
  const input = container.querySelector<HTMLInputElement>('input[aria-label="Workflow name"]');
  expect(input).toBeTruthy();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await waitFor(() => expect(container.textContent).toContain("Unsaved changes"));
}

function prepareBrowserBackTarget() {
  window.history.replaceState({ page: "studio" }, "", "/studio");
  window.history.pushState({ page: "editor" }, "", "/studio/edit?id=workflow-1");
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/studio/edit?id=workflow-1");
  api.getWorkflow.mockResolvedValue(workflow);
  api.listNodeTypes.mockResolvedValue([]);
  api.listConnectors.mockResolvedValue([]);
  api.listManifests.mockResolvedValue([]);
  api.listContracts.mockResolvedValue([]);
  api.listWorkflows.mockResolvedValue([]);
  api.listRuns.mockResolvedValue({ runs: [] });
  api.getHealth.mockResolvedValue({ graph_version_ref: null });
  api.getRun.mockResolvedValue({ ...workflow, run_id: "run-1", status: "completed" });
  api.getRunTimeline.mockResolvedValue({ entries: [] });
  api.updateWorkflow.mockResolvedValue(workflow);
  api.publishWorkflow.mockResolvedValue(workflow);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("studio editor safety", () => {
  it("keeps the node dialog and graph visible when deletion is refused", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountEditor();

    await act(async () => {
      button("node:worker")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });
    expect(container.querySelector('[role="dialog"]')).toBeTruthy();

    await act(async () => button("Delete node")?.click());

    expect(confirm).toHaveBeenCalledWith(
      "Delete workflow elements?\nNodes: Worker (worker)\nEdges: handoff\nThis cannot be undone.",
    );
    expect(container.querySelector('[role="dialog"]')).toBeTruthy();
    expect(button("node:worker")).toBeTruthy();
  });

  it("keeps browser Back on the editor and restores the guard when navigation is refused", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const traversed: string[] = [];
    const observePop = () => traversed.push(`${window.location.pathname}${window.location.search}`);
    prepareBrowserBackTarget();
    window.addEventListener("popstate", observePop);
    await mountEditor();
    await changeWorkflowName("Changed workflow");

    window.history.back();
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));

    expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
    expect(traversed).toEqual(["/studio/edit?id=workflow-1"]);
    expect(window.location.pathname).toBe("/studio/edit");
    expect(container.textContent).toContain("Unsaved changes");

    window.history.back();
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));

    expect(traversed).toEqual([
      "/studio/edit?id=workflow-1",
      "/studio/edit?id=workflow-1",
    ]);
    expect(window.location.pathname).toBe("/studio/edit");

    window.removeEventListener("popstate", observePop);
    confirm.mockReturnValue(true);
    window.history.back();
    await waitFor(() => expect(window.location.pathname).toBe("/studio"));
  });

  it("continues browser Back once without a second prompt while saving", async () => {
    api.updateWorkflow.mockImplementation(() => new Promise(() => {}));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const traversed: string[] = [];
    const observePop = () => traversed.push(`${window.location.pathname}${window.location.search}`);
    prepareBrowserBackTarget();
    window.addEventListener("popstate", observePop);
    await mountEditor();
    await changeWorkflowName("Saving workflow");
    await act(async () => button("Save")?.click());
    await waitFor(() => expect(container.textContent).toContain("Saving…"));

    window.history.back();
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));

    const unload = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unload)).toBe(true);
    await waitFor(() => expect(window.location.pathname).toBe("/studio"));

    expect(confirm).toHaveBeenCalledWith("Discard unsaved workflow changes?");
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(traversed).toEqual(["/studio/edit?id=workflow-1", "/studio"]);
    window.removeEventListener("popstate", observePop);
  });

  it("keeps a failed save visible and refuses to publish the stale draft", async () => {
    api.updateWorkflow.mockRejectedValue(new Error("save offline"));
    await mountEditor();

    await act(async () => button("Publish")?.click());
    await waitFor(() => expect(container.textContent).toContain("Error: save offline"));

    expect(api.publishWorkflow).not.toHaveBeenCalled();
    expect(button("Publish")).toBeTruthy();
    expect(button("node:worker")).toBeTruthy();
  });

  it("deletes a selected node from the keyboard only after confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await mountEditor();

    await act(async () => button("node:worker")?.click());
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));

    expect(confirm).toHaveBeenCalledWith(
      "Delete workflow elements?\nNodes: Worker (worker)\nEdges: handoff\nThis cannot be undone.",
    );
    expect(button("node:worker")).toBeUndefined();
    expect(button("edge:handoff")).toBeUndefined();
  });

  it("keeps a selected edge when keyboard deletion is refused", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountEditor();

    await act(async () => button("edge:handoff")?.click());
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));

    expect(confirm).toHaveBeenCalledWith(
      "Delete workflow elements?\nNodes: none\nEdges: handoff\nThis cannot be undone.",
    );
    expect(button("edge:handoff")).toBeTruthy();
    expect(button("node:worker")).toBeTruthy();
    expect(button("node:reviewer")).toBeTruthy();
  });

  it("deletes a selected edge from the keyboard after confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await mountEditor();

    await act(async () => button("edge:handoff")?.click());
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));

    expect(confirm).toHaveBeenCalledWith(
      "Delete workflow elements?\nNodes: none\nEdges: handoff\nThis cannot be undone.",
    );
    expect(button("edge:handoff")).toBeUndefined();
    expect(button("node:worker")).toBeTruthy();
    expect(button("node:reviewer")).toBeTruthy();
  });

  it("preserves a selected node and unrelated selected edge when the full scope is refused", async () => {
    api.getWorkflow.mockResolvedValue(mixedSelectionWorkflow);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountEditor();

    await act(async () => button("node:worker")?.click());
    await act(async () => button("edge:audit")?.click());
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));

    const prompt = confirm.mock.calls[0]?.[0];
    expect(prompt).toContain("Worker (worker)");
    expect(prompt).toContain("handoff");
    expect(prompt).toContain("audit");
    expect(button("node:worker")).toBeTruthy();
    expect(button("edge:handoff")).toBeTruthy();
    expect(button("edge:audit")).toBeTruthy();
  });

  it("deletes every disclosed edge with an accepted mixed node and edge scope", async () => {
    api.getWorkflow.mockResolvedValue(mixedSelectionWorkflow);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await mountEditor();

    await act(async () => button("node:worker")?.click());
    await act(async () => button("edge:audit")?.click());
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete" })));

    const prompt = confirm.mock.calls[0]?.[0];
    expect(prompt).toContain("Worker (worker)");
    expect(prompt).toContain("handoff");
    expect(prompt).toContain("audit");
    expect(button("node:worker")).toBeUndefined();
    expect(button("edge:handoff")).toBeUndefined();
    expect(button("edge:audit")).toBeUndefined();
    expect(button("node:reviewer")).toBeTruthy();
    expect(button("node:auditor")).toBeTruthy();
  });

  it("keeps the newest past-run response when requests resolve out of order", async () => {
    let resolveFirst!: (value: object) => void;
    let resolveSecond!: (value: object) => void;
    const first = new Promise<object>((resolve) => {
      resolveFirst = resolve;
    });
    const second = new Promise<object>((resolve) => {
      resolveSecond = resolve;
    });
    api.listRuns.mockResolvedValue({
      runs: [
        { ...workflow, run_id: "past-1", status: "completed", current_step: null },
        { ...workflow, run_id: "past-2", status: "completed", current_step: null },
      ],
    });
    api.getRun.mockImplementation((id: string) => (id === "past-1" ? first : second));
    await mountEditor();

    await act(async () => buttonContaining("▶")?.click());
    await waitFor(() => expect(buttonContaining("Past runs")).toBeTruthy());
    await act(async () => buttonContaining("Past runs")?.click());
    await waitFor(() => expect(buttonContaining("past-2")).toBeTruthy());
    await act(async () => buttonContaining("past-1")?.click());
    await act(async () => buttonContaining("past-2")?.click());
    await act(async () =>
      resolveSecond({ ...workflow, run_id: "past-2", status: "completed", current_step: "newest-state" }),
    );
    await waitFor(() => expect(container.textContent).toContain("newest-state"));

    await act(async () =>
      resolveFirst({ ...workflow, run_id: "past-1", status: "completed", current_step: "stale-state" }),
    );

    expect(container.textContent).toContain("newest-state");
    expect(container.textContent).not.toContain("stale-state");
  });
});
