// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@xyflow/react", () => ({
  Handle: ({ children, ...props }: { children?: ReactNode }) => <span {...props}>{children}</span>,
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
}));
vi.mock("@/app/components/runState", () => ({
  useNodeIssue: () => undefined,
  useNodeRunState: () => undefined,
}));
vi.mock("@/app/components/ModelRightsizing", () => ({ ModelRightsizing: () => null }));

import { NodeInspector } from "./NodeInspector";
import { StudioNodeView } from "./StudioNodeView";

const config = {
  server_ref: "filesystem",
  tool_name: "read_file",
  description: "Read a file",
  input_schema: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
  schema_hash: "0123456789abcdef".repeat(4),
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("MCP tool node presentation", () => {
  it("renders identity, schema pin, and delivery semantics on the canvas", async () => {
    await act(async () => {
      root.render(<StudioNodeView {...({ id: "fs", selected: false, data: {
        label: "Read file", studioType: "mcp_tool", config,
        ports: [{ id: "tool-input", type: "tool", direction: "input", label: "Tool" }],
      }} as unknown as Parameters<typeof StudioNodeView>[0])} />);
    });
    expect(container.textContent).toContain("filesystem / read_file");
    expect(container.textContent).toContain("Pinned");
    expect(container.textContent).toContain("At least once");
  });

  it("shows the pinned schema in the inspector", async () => {
    await act(async () => {
      root.render(<NodeInspector studioType="mcp_tool" label="Read file" config={config}
        inputContractRef={null} outputContractRef={null} contractOptions={["contract://input"]}
        onLabelChange={() => undefined} onConfigChange={() => undefined}
        onContractRefChange={() => undefined} />);
    });
    expect(container.textContent).toContain("Pinned input schema");
    expect(container.textContent).toContain('"path"');
    expect(container.textContent).not.toContain("Input contract");
  });

  it("renders an attached MCP contract as imported and read-only", async () => {
    await act(async () => {
      root.render(<NodeInspector studioType="agent" label="Researcher"
        config={{ instruction: "Use it", model_provider: "openai/gpt-4o-mini", tool_bindings: [{
          target_node_id: "fs", name: "read_file", description: "Read a file", arguments: [],
        }] }}
        toolTargets={[{ id: "fs", label: "Read file", studioType: "mcp_tool", config }]}
        onLabelChange={() => undefined} onConfigChange={() => undefined} />);
    });
    expect(container.textContent).toContain("Pinned MCP contract");
    expect(container.textContent).toContain("filesystem / read_file");
    expect(container.querySelector('input[value="read_file"]')).toBeNull();
    expect(container.textContent).not.toContain("+ Add argument");
  });
});
