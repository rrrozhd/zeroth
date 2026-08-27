// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import { TraversalEvidence } from "./TraversalEvidence";

describe("TraversalEvidence", () => {
  function renderTraversal(element: React.ReactElement) {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => root.render(element));
    return { container, cleanup: () => act(() => { root.unmount(); container.remove(); }) };
  }

  it("renders loop iteration counts and content-free routing decisions", () => {
    const view = renderTraversal(
      <TraversalEvidence
        traversal={{
          node_visit_counts: { research: 2, review: 1 },
          edge_visit_counts: { "review-to-research": 1 },
          routing_decisions: [
            {
              condition_id: "continue-loop",
              selected_edge_id: "review-to-research",
              matched: true,
              suppression_reason: null,
            },
          ],
          stop_reason: "branch_suppressed",
        }}
      />,
    );

    expect(view.container.textContent).toContain("research");
    expect(view.container.textContent).toContain("2 visits");
    expect(view.container.textContent).toContain("continue-loop");
    expect(view.container.textContent).toContain("matched");
    expect(view.container.textContent).toContain("No route condition matched");
    view.cleanup();
  });

  it("distinguishes a safety-limit stop from a normal conditional exit", () => {
    const view = renderTraversal(
      <TraversalEvidence
        traversal={{
          node_visit_counts: { repair: 3 },
          edge_visit_counts: { retry: 2 },
          routing_decisions: [
            {
              condition_id: "retry",
              selected_edge_id: "retry",
              matched: true,
              suppression_reason: "visit_limit",
            },
          ],
          stop_reason: "branch_suppressed",
        }}
      />,
    );

    expect(view.container.textContent).toContain("Safety limit stopped further traversal");
    expect(view.container.textContent).toContain("visit limit");
    view.cleanup();
  });
});
