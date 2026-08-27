// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@xyflow/react", () => ({
  Handle: ({ id, title }: { id: string; title: string }) => (
    <span data-handle={id} title={title} />
  ),
  Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
}));

import { StudioNodeView } from "./StudioNodeView";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("StudioNodeView Loop controller", () => {
  it("shows retry semantics and all named outcomes on the node", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <StudioNodeView
          id="quality-loop"
          type="studio"
          selected={false}
          dragging={false}
          zIndex={0}
          selectable
          deletable
          draggable
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          data={{
            label: "Quality loop",
            studioType: "loop",
            config: { max_retries: 3 },
            ports: [
              { id: "input-data", type: "data", direction: "input", label: "Input" },
              { id: "repeat", type: "data", direction: "output", label: "Repeat" },
              { id: "done", type: "data", direction: "output", label: "Done" },
              { id: "limit", type: "data", direction: "output", label: "Limit" },
            ],
          }}
        />,
      );
    });

    expect(container.textContent).toContain("1 attempt + 3 retries");
    expect(container.textContent).toContain("Repeat");
    expect(container.textContent).toContain("Done");
    expect(container.textContent).toContain("Limit");
    expect(container.querySelector(".studio-node-card")?.className).toContain("is-loop");

    await act(async () => root.unmount());
  });

  it("renders If as an explicit two-route control node", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <StudioNodeView
          id="quality-gate"
          type="studio"
          selected={false}
          dragging={false}
          zIndex={0}
          selectable
          deletable
          draggable
          isConnectable
          positionAbsoluteX={0}
          positionAbsoluteY={0}
          data={{
            label: "Quality gate",
            studioType: "if",
            config: { expression: "payload.score >= 0.8" },
            ports: [
              { id: "input-data", type: "data", direction: "input", label: "Input" },
              { id: "true", type: "data", direction: "output", label: "True" },
              { id: "false", type: "data", direction: "output", label: "False" },
            ],
          }}
        />,
      );
    });

    expect(container.textContent).toContain("payload.score >= 0.8");
    expect(container.textContent).toContain("True");
    expect(container.textContent).toContain("False");
    expect(container.querySelector(".studio-node-card")?.className).toContain("is-if");

    await act(async () => root.unmount());
  });
});
