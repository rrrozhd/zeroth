// @vitest-environment jsdom

import type { AnchorHTMLAttributes } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listApprovals: vi.fn(),
  resolveApproval: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import ApprovalsPage from "./page";

const pending = {
  approval_id: "approval-child-1",
  run_id: "child-run-1",
  thread_id: "child-thread-1",
  node_id: "subgraph:child:1:approve",
  graph_version_ref: "child-graph:v1",
  deployment_ref: "child-deployment",
  tenant_id: "evaluation-studio-v1",
  workspace_id: null,
  interaction_type: "approval",
  status: "pending",
  requested_decision: "approve",
  allowed_actions: ["approve", "reject"],
  summary: "Review the exact child branch",
  rationale: "Provider-free D-012 acceptance",
  context_excerpt: {},
  proposed_payload: { branch: "approval" },
  urgency_metadata: {},
  resolution: null,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
};

let container: HTMLDivElement;
let root: Root;

async function waitFor(assertion: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      failure = error;
      await act(async () => new Promise((resolve) => window.setTimeout(resolve, 0)));
    }
  }
  throw failure;
}

function button(label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (!match) throw new Error(`missing button ${label}`);
  return match;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  api.listApprovals.mockResolvedValue([pending]);
  api.resolveApproval.mockResolvedValue({ approval: pending, run: { run_id: "parent-run-1" } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("approval decision evidence", () => {
  it("submits a reviewer reason from a stable evidence-addressable child card", async () => {
    await act(async () => root.render(<ApprovalsPage />));
    await waitFor(() => expect(container.querySelector("textarea")).toBeTruthy());

    const card = container.querySelector('[data-evidence-id="approvals.card.approval-child-1"]');
    const reason = container.querySelector(
      '[data-evidence-id="approvals.reason.approval-child-1"]',
    ) as HTMLTextAreaElement;
    expect(card).toBeTruthy();
    expect(reason.getAttribute("aria-label")).toBe("Decision reason");
    expect(button("Approve").dataset.evidenceId).toBe(
      "approvals.approve.approval-child-1",
    );

    await act(async () => {
      const valueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value",
      )?.set;
      valueSetter?.call(reason, "Verified durable sibling delivery before approving");
      reason.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Approve").click());

    await waitFor(() => expect(api.resolveApproval).toHaveBeenCalledWith("approval-child-1", {
      decision: "approve",
      reason: "Verified durable sibling delivery before approving",
    }));
  });
});
