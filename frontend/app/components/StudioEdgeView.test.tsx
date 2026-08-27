// @vitest-environment jsdom

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@xyflow/react", () => ({
  BaseEdge: ({ path, className }: { path: string; className?: string }) => (
    <svg><path data-testid="edge-path" d={path} className={className} /></svg>
  ),
  EdgeLabelRenderer: ({ children }: { children: ReactNode }) => <>{children}</>,
  getSmoothStepPath: () => ["M 0 0 L 100 0", 50, 0],
}));

import { StudioEdgeView } from "./StudioEdgeView";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("StudioEdgeView", () => {
  it("renders a loop return as an unlabelled outer route", async () => {
    await act(async () => {
      root.render(
        <StudioEdgeView
          id="repair-inspect"
          source="repair"
          target="inspect"
          sourceX={850}
          sourceY={180}
          targetX={320}
          targetY={300}
          sourcePosition={"right" as never}
          targetPosition={"left" as never}
          markerEnd="arrow"
          data={{
            kind: "data",
            enabled: true,
            presentation: {
              role: "loop-return",
              label: "Loop back",
              detail: "Recheck after repair",
            } as never,
          }}
        />,
      );
    });

    expect(container.textContent).not.toContain("Loop back");
    expect(container.textContent).not.toContain("Recheck after repair");
    expect(container.querySelector(".studio-loop-frame")).toBeNull();
    expect(container.querySelector("[data-testid='edge-path']")?.getAttribute("d")).toContain("M 850 180");
    expect(container.querySelector(".studio-edge-label")).toBeNull();
  });

  it("never renders condition or route tags on the canvas", async () => {
    await act(async () => {
      root.render(
        <StudioEdgeView
          id="decision-true"
          source="decision"
          target="next"
          sourceX={0}
          sourceY={0}
          targetX={100}
          targetY={0}
          sourcePosition={"right" as never}
          targetPosition={"left" as never}
          markerEnd="arrow"
          data={{
            kind: "data",
            enabled: true,
            presentation: {
              role: "conditional",
              label: "When",
              detail: "Score ≥ 0.8",
            } as never,
          }}
        />,
      );
    });

    expect(container.textContent).not.toContain("When");
    expect(container.textContent).not.toContain("Score");
    expect(container.querySelector(".studio-edge-label")).toBeNull();
  });
});
