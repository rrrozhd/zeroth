// @vitest-environment jsdom

import type { AnchorHTMLAttributes, ReactNode } from "react";
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getWorkflow: vi.fn(),
  listNodeTypes: vi.fn(),
  listConnectors: vi.fn(),
  listManifests: vi.fn(),
  listContracts: vi.fn(),
  listTemplates: vi.fn(),
  listWorkflows: vi.fn(),
  listRuns: vi.fn(),
  getHealth: vi.fn(),
  getInputContract: vi.fn(),
  getRun: vi.fn(),
  getRunTimeline: vi.fn(),
  submitRun: vi.fn(),
  updateWorkflow: vi.fn(),
  publishWorkflow: vi.fn(),
  preflightWorkflow: vi.fn(),
  verifyWorkflowProviders: vi.fn(),
}));

const flow = vi.hoisted(() => ({
  fitView: vi.fn(),
  getViewport: vi.fn(() => ({ x: 0, y: 0, zoom: 1 })),
  screenToFlowPosition: vi.fn((position: { x: number; y: number }) => position),
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
    onNodeClick?: (event: MouseEvent, node: { id: string }) => void;
    nodeClickDistance?: number;
    onEdgeDoubleClick: (event: MouseEvent, edge: { id: string }) => void;
    onPaneClick?: (event: MouseEvent) => void;
    onInit?: (instance: typeof flow) => void;
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
      onNodeClick,
      nodeClickDistance,
      onEdgeDoubleClick,
      onPaneClick,
      onInit,
      children,
    }: FlowProps) => {
      useEffect(() => {
        onInit?.(flow);
      }, [onInit]);
      return <div
        aria-label="Workflow graph editor"
        data-node-click-distance={nodeClickDistance}
      >
        {nodes.map((node) => (
          <button
            key={node.id}
            aria-pressed={node.selected ?? false}
            onClick={(event) => {
              onNodesChange([{ id: node.id, type: "select", selected: true }]);
              onNodeClick?.(event.nativeEvent, node);
            }}
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
            onDoubleClick={(event) => onEdgeDoubleClick(event.nativeEvent, edge)}
          >
            edge:{edge.id}
          </button>
        ))}
        <button onClick={(event) => onPaneClick?.(event.nativeEvent)}>Canvas</button>
        {children}
      </div>;
    },
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    Panel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  };
});

import { assignEvidenceIdentities } from "@/app/lib/evidence-identity";
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
  execution_settings: {
    max_total_steps: 1000,
    max_total_runtime_seconds: null,
    max_visits_per_node: 10,
    max_visits_per_edge: null,
    default_timeout_seconds: null,
    failure_policy: "fail_fast",
    audit_enabled: true,
    sequential_join_enabled: false,
  },
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

const advancedWorkflow = {
  ...workflow,
  nodes: [
    {
      ...workflow.nodes[0],
      data: {
        ...workflow.nodes[0].data,
        parallel_config: {
          split_path: "items",
          merge_strategy: "collect",
          fail_mode: "best_effort",
          max_branches: 10,
          max_concurrency: 3,
          batch_size: 2,
          branch_timeout_seconds: 5,
        },
      },
    },
    {
      ...workflow.nodes[1],
      data: {
        ...workflow.nodes[1].data,
        join_config: { merge_strategy: "collect", merge_path: "results" },
      },
    },
  ],
  edges: [
    {
      ...workflow.edges[0],
      condition: {
        expression: "payload.iteration < 3",
        operand_refs: ["payload.iteration"],
        branch_rule: "expression",
        allow_cycle_traversal: true,
      },
      mapping: {
        operations: [
          {
            operation: "rename",
            source_path: "payload.items",
            target_path: "items",
          },
        ],
      },
      enabled: false,
    },
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
  return Array.from(document.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
}

function buttonContaining(label: string): HTMLButtonElement | undefined {
  return Array.from(document.querySelectorAll("button")).find((candidate) =>
    candidate.textContent?.includes(label),
  );
}

function evidenceControl<T extends HTMLElement>(identity: string): T {
  const control = document.querySelector<T>(`[data-evidence-id="${identity}"]`);
  expect(control).toBeTruthy();
  return control!;
}

async function setNativeValue(
  control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement,
  value: string,
) {
  await act(async () => {
    const prototype = control instanceof window.HTMLSelectElement
      ? window.HTMLSelectElement.prototype
      : control instanceof window.HTMLTextAreaElement
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(control, value);
    control.dispatchEvent(new Event(control instanceof window.HTMLSelectElement ? "change" : "input", {
      bubbles: true,
    }));
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
  api.listTemplates.mockResolvedValue({ templates: [] });
  api.listWorkflows.mockResolvedValue([]);
  api.listRuns.mockResolvedValue({ runs: [] });
  api.getHealth.mockResolvedValue({ graph_version_ref: null });
  api.getInputContract.mockResolvedValue({
    name: "workflow-input",
    version: 1,
    json_schema: { type: "object", properties: {} },
  });
  api.getRun.mockResolvedValue({ ...workflow, run_id: "run-1", status: "completed" });
  api.getRunTimeline.mockResolvedValue({ entries: [] });
  api.submitRun.mockResolvedValue({ run_id: "run-1", status: "queued", thread_id: "thread-1" });
  api.updateWorkflow.mockResolvedValue(workflow);
  api.publishWorkflow.mockResolvedValue(workflow);
  api.preflightWorkflow.mockResolvedValue({
    workflow_id: "workflow-1",
    version: 1,
    ready: true,
    checks: ["static_validation", "contracts"],
    issues: [],
  });
  api.verifyWorkflowProviders.mockResolvedValue({
    workflow_id: "workflow-1",
    verified: true,
    probes: [{ model: "openai/gpt-4o-mini", ok: true, latency_ms: 20 }],
  });
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
  it("authors and restores nested context-window and thread settings", async () => {
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    await waitFor(() => expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.enabled"]',
    )).toBeTruthy());

    const enabled = evidenceControl<HTMLInputElement>("studio.agent.context-window.enabled");
    expect(enabled.checked).toBe(false);
    expect(document.querySelector('[data-evidence-id="studio.agent.context-window.strategy"]')).toBeNull();

    await act(async () => enabled.click());

    const strategy = evidenceControl<HTMLSelectElement>("studio.agent.context-window.strategy");
    expect(Array.from(strategy.options, (option) => option.value)).toEqual([
      "observation_masking",
      "truncation",
      "llm_summarization",
    ]);
    for (const value of ["truncation", "observation_masking", "llm_summarization"]) {
      await setNativeValue(strategy, value);
      expect(strategy.value).toBe(value);
    }
    expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.llm-summarization-notice"]',
    )).toBeTruthy();

    const maxTokens = evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.max-context-tokens",
    );
    const trigger = evidenceControl<HTMLInputElement>("studio.agent.context-window.trigger-ratio");
    const preserve = evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.preserve-recent-messages",
    );
    expect(maxTokens.min).toBe("0");
    expect(trigger.min).toBe("0");
    expect(trigger.max).toBe("1");
    expect(trigger.step).toBe("any");
    expect(preserve.min).toBe("0");

    await setNativeValue(maxTokens, "64000");
    await setNativeValue(trigger, "0.75");
    await setNativeValue(preserve, "6");
    await act(async () => evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.archive-originals",
    ).click());

    const messageKey = evidenceControl<HTMLInputElement>("studio.agent.input-messages-key");
    const persistConversation = evidenceControl<HTMLInputElement>(
      "studio.agent.persist-conversation",
    );
    expect(document.querySelector(
      '[data-evidence-id="studio.agent.conversation-max-turns"]',
    )).toBeNull();
    await setNativeValue(messageKey, "messages");
    await act(async () => persistConversation.click());
    const maxTurns = evidenceControl<HTMLInputElement>("studio.agent.conversation-max-turns");
    await setNativeValue(maxTurns, "25");

    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());
    const savedConfig = api.updateWorkflow.mock.calls.at(-1)?.[1].nodes[0].data.config;
    expect(savedConfig).toMatchObject({
      context_window: {
        max_context_tokens: 64000,
        summary_trigger_ratio: 0.75,
        compaction_strategy: "llm_summarization",
        preserve_recent_messages_count: 6,
        archive_originals: true,
      },
      input_messages_key: "messages",
      persist_conversation: true,
      conversation_max_turns: 25,
    });

    api.getWorkflow.mockResolvedValue({
      ...workflow,
      nodes: [
        {
          ...workflow.nodes[0],
          data: { ...workflow.nodes[0].data, config: savedConfig },
        },
        workflow.nodes[1],
      ],
    });
    await act(async () => root.unmount());
    root = createRoot(container);
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    await waitFor(() => expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.enabled"]',
    )).toBeTruthy());

    expect(evidenceControl<HTMLInputElement>("studio.agent.context-window.enabled").checked).toBe(true);
    expect(evidenceControl<HTMLSelectElement>("studio.agent.context-window.strategy").value).toBe(
      "llm_summarization",
    );
    expect(evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.max-context-tokens",
    ).value).toBe("64000");
    expect(evidenceControl<HTMLInputElement>("studio.agent.conversation-max-turns").value).toBe(
      "25",
    );
  });

  it("rejects invalid context-window bounds without persisting them", async () => {
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    await waitFor(() => expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.enabled"]',
    )).toBeTruthy());
    await act(async () => evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.enabled",
    ).click());

    const maxTokens = evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.max-context-tokens",
    );
    await setNativeValue(maxTokens, "64000");
    await setNativeValue(maxTokens, "-1");
    expect(maxTokens.getAttribute("aria-invalid")).toBe("true");
    expect(document.body.textContent).toContain("Use a whole number of 0 or more.");

    const trigger = evidenceControl<HTMLInputElement>("studio.agent.context-window.trigger-ratio");
    await setNativeValue(trigger, "0.001");
    await setNativeValue(trigger, "0");
    expect(trigger.getAttribute("aria-invalid")).toBe("true");
    expect(document.body.textContent).toContain("Use a ratio greater than 0 and no more than 1.");
    await setNativeValue(trigger, "1.1");
    expect(trigger.getAttribute("aria-invalid")).toBe("true");

    const preserve = evidenceControl<HTMLInputElement>(
      "studio.agent.context-window.preserve-recent-messages",
    );
    await setNativeValue(preserve, "6");
    await setNativeValue(preserve, "-1");
    expect(preserve.getAttribute("aria-invalid")).toBe("true");

    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());
    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].nodes[0].data.config.context_window).toEqual({
      max_context_tokens: 64000,
      summary_trigger_ratio: 0.001,
      compaction_strategy: "observation_masking",
      preserve_recent_messages_count: 6,
      archive_originals: false,
    });
    expect(document.body.textContent).toContain("Fix this value before changing context settings again");
  });

  it("removes disabled context-window settings and locks published controls", async () => {
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    await waitFor(() => expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.enabled"]',
    )).toBeTruthy());
    const enabled = evidenceControl<HTMLInputElement>("studio.agent.context-window.enabled");
    await act(async () => enabled.click());
    await act(async () => enabled.click());
    expect(document.querySelector('[data-evidence-id="studio.agent.context-window.strategy"]')).toBeNull();
    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());
    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].nodes[0].data.config).not.toHaveProperty(
      "context_window",
    );

    api.getWorkflow.mockResolvedValue({
      ...workflow,
      status: "published",
      nodes: [
        {
          ...workflow.nodes[0],
          data: {
            ...workflow.nodes[0].data,
            config: {
              context_window: {
                max_context_tokens: 32000,
                summary_trigger_ratio: 1,
                compaction_strategy: "truncation",
                preserve_recent_messages_count: 0,
                archive_originals: false,
              },
              input_messages_key: "messages",
              persist_conversation: true,
              conversation_max_turns: 1,
            },
          },
        },
        workflow.nodes[1],
      ],
    });
    await act(async () => root.unmount());
    root = createRoot(container);
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    await waitFor(() => expect(document.querySelector(
      '[data-evidence-id="studio.agent.context-window.enabled"]',
    )).toBeTruthy());

    const identities = [
      "studio.agent.context-window.enabled",
      "studio.agent.context-window.max-context-tokens",
      "studio.agent.context-window.trigger-ratio",
      "studio.agent.context-window.strategy",
      "studio.agent.context-window.preserve-recent-messages",
      "studio.agent.context-window.archive-originals",
      "studio.agent.input-messages-key",
      "studio.agent.persist-conversation",
      "studio.agent.conversation-max-turns",
    ];
    expect(identities.every((identity) => evidenceControl<HTMLInputElement>(identity).disabled)).toBe(
      true,
    );
    expect(document.body.textContent).toContain("Read-only (published)");
  });

  it("authors a pinned prompt template and memory binding through the agent inspector", async () => {
    api.listTemplates.mockResolvedValue({
      templates: [
        {
          name: "grounded-answer",
          version: 1,
          template_str: "Answer {{ question }} with {{ memory.policy }}",
          variables: ["question", "memory"],
          description: "Grounded response",
        },
        {
          name: "grounded-answer",
          version: 2,
          template_str: "Answer {{ question }} with {{ memory.policy }}",
          variables: ["question", "memory"],
          description: "Grounded response",
        },
      ],
    });
    api.listConnectors.mockResolvedValue([
      { ref: "memory://policies", backend: "key_value", params: {} },
    ]);
    await mountEditor();

    await act(async () => button("node:worker")?.click());

    await waitFor(() => {
      const control = document.querySelector<HTMLSelectElement>(
        '[data-evidence-id="studio.agent.template.name"]',
      );
      expect(control).toBeTruthy();
    });
    const templateName = document.querySelector<HTMLSelectElement>(
      '[data-evidence-id="studio.agent.template.name"]',
    );
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(
        templateName,
        "grounded-answer",
      );
      templateName!.dispatchEvent(new Event("change", { bubbles: true }));
    });

    const templateVersion = document.querySelector<HTMLSelectElement>(
      '[data-evidence-id="studio.agent.template.version"]',
    );
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(
        templateVersion,
        "1",
      );
      templateVersion?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => button("Add memory binding")?.click());

    const alias = document.querySelector<HTMLInputElement>(
      '[data-evidence-id="studio.agent.template.memory.0.alias"]',
    );
    const key = document.querySelector<HTMLInputElement>(
      '[data-evidence-id="studio.agent.template.memory.0.key"]',
    );
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        alias,
        "policy",
      );
      alias?.dispatchEvent(new Event("input", { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        key,
        "refunds/current",
      );
      key?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());

    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].nodes[0].data.config).toMatchObject({
      template_ref: { name: "grounded-answer", version: 1 },
      memory_refs: ["memory://policies"],
      template_memory_bindings: [
        {
          as_name: "policy",
          connector_instance_id: "memory://policies",
          access_mode: "get",
          key: "refunds/current",
          scope: "run",
        },
      ],
    });
  });

  it("keeps published template bindings visible and read-only", async () => {
    api.getWorkflow.mockResolvedValue({
      ...workflow,
      status: "published",
      nodes: [
        {
          ...workflow.nodes[0],
          data: {
            ...workflow.nodes[0].data,
            config: {
              template_ref: { name: "grounded-answer", version: 2 },
              memory_refs: ["memory://policies"],
              template_memory_bindings: [
                {
                  as_name: "policy",
                  connector_instance_id: "memory://policies",
                  access_mode: "get",
                  key: "refunds/current",
                  scope: "run",
                },
              ],
            },
          },
        },
        workflow.nodes[1],
      ],
    });
    api.listTemplates.mockResolvedValue({
      templates: [
        {
          name: "grounded-answer",
          version: 2,
          template_str: "Answer {{ question }}",
          variables: ["question"],
          description: "Grounded response",
        },
      ],
    });
    api.listConnectors.mockResolvedValue([
      { ref: "memory://policies", backend: "key_value", params: {} },
    ]);
    await mountEditor();

    await act(async () => button("node:worker")?.click());

    const controls = Array.from(
      document.querySelectorAll<HTMLElement>('[data-evidence-id^="studio.agent.template."]'),
    ).filter((control) => control.matches("input, select, button, textarea"));
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((control) => control.hasAttribute("disabled"))).toBe(true);
    expect(document.body.textContent).toContain("Pinned to version 2");
    expect(button("Add memory binding")).toBeUndefined();
    expect(button("Remove memory binding 1")).toBeUndefined();
  });

  it("explains role-restricted template access without dropping a saved reference", async () => {
    api.getWorkflow.mockResolvedValue({
      ...workflow,
      nodes: [
        {
          ...workflow.nodes[0],
          data: {
            ...workflow.nodes[0].data,
            config: { template_ref: { name: "review-only", version: 3 } },
          },
        },
        workflow.nodes[1],
      ],
    });
    api.listTemplates.mockRejectedValue(new Error("403 forbidden"));
    await mountEditor();

    await act(async () => button("node:worker")?.click());

    expect(document.body.textContent).toContain("Template library access is restricted for this role");
    const name = document.querySelector<HTMLInputElement>(
      '[data-evidence-id="studio.agent.template.name"]',
    );
    expect(name?.value).toBe("review-only");
    expect(name?.disabled).toBe(true);
  });

  it("fits the full graph after a compact editor finishes loading", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockReturnValue({ matches: true }),
    });

    await mountEditor();

    await waitFor(() => expect(flow.fitView).toHaveBeenCalledWith({ maxZoom: 1, padding: 0.25 }));
  });

  it("never traps navigation from an immutable published workflow", async () => {
    api.getWorkflow.mockResolvedValue({ ...workflow, status: "published" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountEditor();

    const studioLink = Array.from(container.querySelectorAll("a")).find(
      (candidate) => candidate.textContent === "← Studio",
    );
    const allowed = studioLink?.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }),
    );

    expect(allowed).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("keeps one evidence-addressable action tree instead of duplicate desktop and compact controls", async () => {
    api.getWorkflow.mockResolvedValue({ ...workflow, status: "published" });
    await mountEditor();

    const result = assignEvidenceIdentities(container, "/console/studio/edit/");

    expect(result.errors).toEqual([]);
    expect(
      container.querySelector('[data-evidence-id="studio.editor.back-to-list"]'),
    ).toBeTruthy();
    expect(container.querySelectorAll('[data-evidence-id="studio.workflow.more-actions"]')).toHaveLength(1);
    await act(async () => button("More")?.click());
    expect(container.querySelectorAll('[data-evidence-id="studio.workflow.clone-to-draft"]')).toHaveLength(1);
  });

  it("shows preflight evidence separately from publish and live verification", async () => {
    await mountEditor();

    await act(async () => button("Run preflight")?.click());
    await waitFor(() => expect(api.preflightWorkflow).toHaveBeenCalledWith("workflow-1"));

    expect(container.textContent).toContain("Preflight passed");
    expect(container.textContent).toContain("Not published");
    expect(container.textContent).toContain("Live provider not verified");

    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => button("More")?.click());
    await act(async () => button("Verify providers")?.click());
    await waitFor(() => expect(api.verifyWorkflowProviders).toHaveBeenCalledWith("workflow-1"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("Live provider verified");
  });

  it("recomputes preflight readiness when a published workflow is reopened", async () => {
    api.getWorkflow.mockResolvedValue({ ...workflow, status: "published", version: 4 });

    await mountEditor();

    await waitFor(() =>
      expect(api.preflightWorkflow).toHaveBeenCalledWith("workflow-1"),
    );
    expect(container.textContent).toContain("Preflight passed");
    expect(container.textContent).toContain("published");
    expect(container.textContent).toContain("v4");
  });

  it("opens the top-right node menu and enters placement mode before adding", async () => {
    api.listNodeTypes.mockResolvedValue([
      { type: "agent", label: "Agent", category: "AI", ports: [] },
    ]);
    await mountEditor();

    const addNode = button("Add node");
    expect(addNode?.getAttribute("aria-expanded")).toBe("false");

    await act(async () => addNode?.click());

    expect(addNode?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".studio-node-menu-popover")).toBeTruthy();
    const option = container.querySelector<HTMLButtonElement>('[role="menuitem"]');
    expect(option?.textContent).toContain("Agent");

    await act(async () => option?.click());

    expect(container.textContent).toContain("Place Agent");
    expect(button("node:agent-")).toBeUndefined();

    await act(async () => button("Canvas")?.click());

    expect(container.textContent).not.toContain("Place Agent");
    expect(
      Array.from(container.querySelectorAll("button")).some((candidate) =>
        candidate.textContent?.startsWith("node:agent-"),
      ),
    ).toBe(true);
  });

  it("opens the Add node palette with A without stealing text-entry keystrokes", async () => {
    api.listNodeTypes.mockResolvedValue([
      { type: "agent", label: "Agent", category: "AI", ports: [] },
    ]);
    await mountEditor();

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", {
        key: "a",
        bubbles: true,
        cancelable: true,
      }));
    });

    expect(button("Add node")?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".studio-node-menu-popover")).toBeTruthy();

    await act(async () => button("Add node")?.click());
    const workflowName = container.querySelector<HTMLInputElement>(
      'input[aria-label="Workflow name"]',
    );
    expect(workflowName).toBeTruthy();

    await act(async () => {
      workflowName?.dispatchEvent(new KeyboardEvent("keydown", {
        key: "a",
        bubbles: true,
        cancelable: true,
      }));
    });

    expect(button("Add node")?.getAttribute("aria-expanded")).toBe("false");
  });

  it("does not open the Add node palette behind an active dialog", async () => {
    await mountEditor();
    await act(async () => button("node:worker")?.click());
    expect(document.querySelector('[role="dialog"][aria-modal="true"]')).toBeTruthy();

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", {
        key: "a",
        bubbles: true,
        cancelable: true,
      }));
    });

    expect(container.querySelector(".studio-node-menu-popover")).toBeNull();
  });

  it("leaves A unclaimed for an immutable published workflow", async () => {
    api.getWorkflow.mockResolvedValue({ ...workflow, status: "published" });
    await mountEditor();
    const shortcut = new KeyboardEvent("keydown", {
      key: "a",
      bubbles: true,
      cancelable: true,
    });

    await act(async () => {
      document.dispatchEvent(shortcut);
    });

    expect(shortcut.defaultPrevented).toBe(false);
    expect(container.querySelector(".studio-node-menu-popover")).toBeNull();
  });

  it("keeps only lifecycle actions in the top toolbar and moves authoring to the canvas", async () => {
    await mountEditor();

    const lifecycle = container.querySelector<HTMLElement>(".studio-workflow-actions");
    expect(lifecycle).toBeTruthy();
    expect(lifecycle?.textContent).toContain("Publish");
    expect(lifecycle?.textContent).toContain("More");
    for (const clutter of ["Add node", "Loop safety", "Run preflight", "Verify providers", "Tidy layout", "Save"]) {
      expect(lifecycle?.textContent).not.toContain(clutter);
    }

    for (const identity of [
      "studio.canvas.add-node",
      "studio.canvas.undo",
      "studio.canvas.redo",
      "studio.canvas.tidy",
      "studio.canvas.preflight",
    ]) {
      expect(container.querySelector(`[data-evidence-id="${identity}"]`)).toBeTruthy();
    }
  });

  it("opens centered node configuration from a single node click", async () => {
    await mountEditor();

    expect(container.querySelector(".studio-inspector-panel")).toBeNull();
    expect(
      container.querySelector('[aria-label="Workflow graph editor"]')?.getAttribute("data-node-click-distance"),
    ).toBe("6");

    await act(async () => button("node:worker")?.click());

    const dialog = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Edit Worker"]');
    expect(dialog).toBeTruthy();
    expect(dialog?.parentElement?.classList.contains("items-center")).toBe(true);
    expect(container.querySelector(".studio-inspector-panel")).toBeNull();
    expect(button("node:worker")?.getAttribute("aria-pressed")).toBe("true");

    await act(async () => button("Done")?.click());

    expect(document.querySelector('[role="dialog"][aria-label="Edit Worker"]')).toBeNull();
  });

  it("recaptures Tab focus when Safari leaves it behind the node dialog", async () => {
    await mountEditor();
    const invokingNode = button("node:worker");
    await act(async () => invokingNode?.click());

    const dialog = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Edit Worker"]');
    expect(dialog).toBeTruthy();
    invokingNode?.focus();
    expect(document.activeElement).toBe(invokingNode);

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    });

    expect(dialog?.contains(document.activeElement)).toBe(true);
    expect((document.activeElement as HTMLElement | null)?.getAttribute("aria-label")).toBe("Close");
  });

  it("edits batch execution settings through node configuration", async () => {
    api.getWorkflow.mockResolvedValue(advancedWorkflow);
    await mountEditor();
    await act(async () => {
      button("node:worker")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });
    const dialog = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Edit Worker"]');
    expect(dialog).toBeTruthy();
    expect(dialog?.parentElement?.classList.contains("items-center")).toBe(true);
    expect(dialog?.className).toContain("max-h-[calc(100dvh-2rem)]");
    await act(async () => button("Execution")?.click());
    const batch = document.querySelector<HTMLInputElement>('input[aria-label="Batch size"]');
    expect(batch?.value).toBe("2");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(batch, "4");
      batch?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());
    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].nodes[0].data.parallel_config.batch_size).toBe(4);
  });

  it("preserves legacy edge conditions without exposing redundant condition authoring", async () => {
    api.getWorkflow.mockResolvedValue(advancedWorkflow);
    await mountEditor();
    await act(async () => {
      button("edge:handoff")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });
    expect(container.querySelector('input[aria-label="Edge condition expression"]')).toBeNull();
    expect(container.textContent).toContain("Use an If or Loop node");
    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());
    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].edges[0].condition.expression).toBe("payload.iteration < 3");
  });

  it("edits and saves graph loop safety limits from the GUI", async () => {
    await mountEditor();

    await act(async () => button("More")?.click());
    await act(async () => button("Loop safety")?.click());

    const dialog = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Loop safety limits"]');
    expect(dialog).toBeTruthy();
    expect(dialog?.textContent).toContain("Loop nodes own Repeat, Done, and Limit routing");
    const maxVisits = dialog?.querySelector<HTMLInputElement>(
      'input[aria-label="Maximum visits per node"]',
    );
    expect(maxVisits?.value).toBe("10");

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(maxVisits, "4");
      maxVisits?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Apply limits")?.click());
    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalled());

    expect(api.updateWorkflow.mock.calls.at(-1)?.[1].execution_settings).toMatchObject({
      max_total_steps: 1000,
      max_visits_per_node: 4,
    });
  });

  it("uses default execution settings when a legacy workflow omits them", async () => {
    const { execution_settings: _omitted, ...legacyWorkflow } = workflow;
    api.getWorkflow.mockResolvedValue(legacyWorkflow);
    await mountEditor();
    await changeWorkflowName("Legacy workflow");

    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalledTimes(1));

    expect(api.updateWorkflow.mock.calls[0]?.[1].execution_settings).toEqual({
      max_total_steps: 1000,
      max_total_runtime_seconds: null,
      max_visits_per_node: 10,
      max_visits_per_edge: null,
      default_timeout_seconds: null,
    });
  });

  it("round-trips advanced node and edge execution settings on save", async () => {
    api.getWorkflow.mockResolvedValue(advancedWorkflow);
    api.updateWorkflow.mockResolvedValue(advancedWorkflow);
    await mountEditor();
    await changeWorkflowName("Advanced workflow");

    await act(async () => button("Save")?.click());
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalledTimes(1));

    const payload = api.updateWorkflow.mock.calls[0]?.[1];
    expect(payload.nodes[0].data.parallel_config).toEqual({
      split_path: "items",
      merge_strategy: "collect",
      fail_mode: "best_effort",
      max_branches: 10,
      max_concurrency: 3,
      batch_size: 2,
      branch_timeout_seconds: 5,
    });
    expect(payload.nodes[1].data.join_config).toEqual({
      merge_strategy: "collect",
      merge_path: "results",
    });
    expect(payload.edges[0]).toMatchObject({
      condition: advancedWorkflow.edges[0].condition,
      mapping: advancedWorkflow.edges[0].mapping,
      enabled: false,
    });
  });

  it("saves with Ctrl+S while a workflow field has focus", async () => {
    await mountEditor();
    await changeWorkflowName("Keyboard save");
    api.updateWorkflow.mockClear();
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Workflow name"]');
    expect(input).toBeTruthy();

    const save = new KeyboardEvent("keydown", {
      key: "s",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      input?.dispatchEvent(save);
    });

    expect(save.defaultPrevented).toBe(true);
    await waitFor(() => expect(api.updateWorkflow).toHaveBeenCalledTimes(1));
  });

  it("renders the canvas save shortcut as an accessible tooltip", async () => {
    await mountEditor();

    const saveButton = button("Save");
    expect(saveButton?.getAttribute("data-tooltip")).toContain("⌘ S");
    expect(saveButton?.getAttribute("aria-keyshortcuts")).toContain("Meta+S");
    expect(saveButton?.getAttribute("data-evidence-id")).toBe("studio.canvas.save");
  });

  it("keeps the node dialog and graph visible when deletion is refused", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountEditor();

    await act(async () => {
      button("node:worker")?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();

    await act(async () => button("Delete node")?.click());

    expect(confirm).toHaveBeenCalledWith(
      "Delete workflow elements?\nNodes: Worker (worker)\nEdges: handoff\nThis cannot be undone.",
    );
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
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

    await act(async () => button("Run")?.click());
    await waitFor(() => expect(buttonContaining("Past runs")).toBeTruthy());
    await act(async () => buttonContaining("Past runs")?.click());
    await waitFor(() => expect(buttonContaining("past-2")).toBeTruthy());
    await act(async () => buttonContaining("past-1")?.click());
    await act(async () => buttonContaining("past-2")?.click());
    await act(async () =>
      resolveSecond({ ...workflow, run_id: "past-2", status: "completed", current_step: "newest-state" }),
    );
    await waitFor(() => expect(document.body.textContent).toContain("newest-state"));

    await act(async () =>
      resolveFirst({ ...workflow, run_id: "past-1", status: "completed", current_step: "stale-state" }),
    );

    expect(document.body.textContent).toContain("newest-state");
    expect(document.body.textContent).not.toContain("stale-state");
  });

  it("keeps a newer past-run selection when an older submit resolves", async () => {
    let resolveSubmit!: (value: object) => void;
    api.getHealth.mockResolvedValue({ graph_version_ref: "workflow-1@1" });
    api.submitRun.mockImplementation(
      () =>
        new Promise<object>((resolve) => {
          resolveSubmit = resolve;
        }),
    );
    api.listRuns.mockResolvedValue({
      runs: [
        {
          ...workflow,
          run_id: "past-1",
          status: "completed",
          current_step: "selected-state",
          thread_id: "selected-thread",
        },
      ],
    });
    api.getRun.mockImplementation((id: string) =>
      Promise.resolve({
        ...workflow,
        run_id: id,
        status: "completed",
        current_step: id === "past-1" ? "selected-state" : "stale-submit-state",
        thread_id: id === "past-1" ? "selected-thread" : "stale-submit-thread",
      }),
    );
    await mountEditor();

    await act(async () => button("Run")?.click());
    await waitFor(() => expect(button("Run")?.disabled).toBe(false));
    await act(async () => button("Run")?.click());
    await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(1));

    const thread = document.querySelector<HTMLInputElement>('input[placeholder="(new conversation)"]');
    expect(thread).toBeTruthy();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        thread,
        "newer-thread",
      );
      thread?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => buttonContaining("Past runs")?.click());
    await waitFor(() => expect(buttonContaining("past-1")).toBeTruthy());
    await act(async () => buttonContaining("past-1")?.click());
    await waitFor(() => expect(document.body.textContent).toContain("selected-state"));

    await act(async () =>
      resolveSubmit({
        run_id: "stale-submit",
        status: "queued",
        thread_id: "stale-submit-thread",
      }),
    );

    expect(document.body.textContent).toContain("selected-state");
    expect(document.body.textContent).not.toContain("stale-submit-state");
    expect(thread?.value).toBe("newer-thread");
  });

  it("echoes the served campaign identity when submitting a strict live run", async () => {
    api.getHealth.mockResolvedValue({
      graph_version_ref: "workflow-1@1",
      campaign_id: "evaluation-studio-v1",
    });
    await mountEditor();

    await act(async () => button("Run")?.click());
    await waitFor(() => expect(button("Run")?.disabled).toBe(false));
    await act(async () => button("Run")?.click());
    await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(1));

    expect(api.submitRun).toHaveBeenCalledWith(
      expect.objectContaining({ campaign_id: "evaluation-studio-v1" }),
    );
  });

  it("restores the exact visible run identity with a stable evidence selector", async () => {
    window.sessionStorage.setItem(
      "zeroth.studio.runPanel.workflow-1",
      JSON.stringify({ runId: "run-refresh-1", payload: "{}" }),
    );
    api.getHealth.mockResolvedValue({ graph_version_ref: "workflow-1@1" });
    api.getRun.mockResolvedValue({
      ...workflow,
      run_id: "run-refresh-1",
      status: "completed",
    });
    api.getRunTimeline.mockResolvedValue({ entries: [] });

    await mountEditor();

    await waitFor(() =>
      expect(
        document.querySelector('[data-evidence-id="studio.run.current-id"]')
          ?.textContent,
      ).toBe("run-refresh-1"),
    );
  });

  it("opens Run as a focused modal and presents failure state only once", async () => {
    window.sessionStorage.setItem(
      "zeroth.studio.runPanel.workflow-1",
      JSON.stringify({ runId: "run-failed-1", threadId: "thread-1", payload: "{}" }),
    );
    api.getHealth.mockResolvedValue({ graph_version_ref: "workflow-1@1" });
    api.getRun.mockResolvedValue({
      ...workflow,
      run_id: "run-failed-1",
      thread_id: "thread-1",
      status: "failed",
      current_step: "approval",
      failure_state: {
        reason: "approval_rejected",
        message: "approval rejected",
        details: {},
      },
    });

    await mountEditor();

    const dialog = document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="Run workflow"]',
    );
    expect(dialog).toBeTruthy();
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    expect(dialog?.parentElement?.classList.contains("studio-dialog-backdrop")).toBe(true);
    expect(dialog?.textContent).toContain("Run run-failed-1");
    expect(dialog?.textContent).toContain("Thread thread-1");
    expect(dialog?.textContent).toContain("Failure details");
    expect((dialog?.textContent?.match(/Failed/g) ?? [])).toHaveLength(1);
  });

  it("does not repeat a service-minted thread identity in the run summary", async () => {
    window.sessionStorage.setItem(
      "zeroth.studio.runPanel.workflow-1",
      JSON.stringify({ runId: "run-minted-thread", threadId: "run-minted-thread", payload: "{}" }),
    );
    api.getHealth.mockResolvedValue({ graph_version_ref: "workflow-1@1" });
    api.getRun.mockResolvedValue({
      ...workflow,
      run_id: "run-minted-thread",
      thread_id: "run-minted-thread",
      status: "completed",
    });

    await mountEditor();

    const summary = document.querySelector('[data-evidence-id="studio.run.summary"]');
    await waitFor(() => expect(summary?.textContent).toContain("Run run-minted-thread"));
    expect(summary?.textContent).not.toContain("Thread run-minted-thread");
  });

  it("prefills the run payload from the served deployment input contract", async () => {
    api.getHealth.mockResolvedValue({
      deployment_ref: "workflow-1-v1",
      graph_version_ref: "workflow-1@1",
      campaign_id: "evaluation-studio-v1",
    });
    api.getInputContract.mockResolvedValue({
      name: "research-query",
      version: 1,
      json_schema: {
        type: "object",
        required: ["query"],
        properties: { query: { type: "string", minLength: 1 } },
      },
    });
    await mountEditor();

    await act(async () => button("Run")?.click());
    const input = document.querySelector<HTMLTextAreaElement>(
      'textarea[data-evidence-id="studio.run.input-payload"]',
    );
    await waitFor(() => expect(input?.value).toContain('"query": "example"'));

    expect(api.getInputContract).toHaveBeenCalledWith("workflow-1-v1");
    expect(input?.value).not.toContain('"question"');
  });

  it("replaces the legacy generic question payload after an existing session reload", async () => {
    window.sessionStorage.setItem(
      "zeroth.studio.runPanel.workflow-1",
      JSON.stringify({
        payload: '{\n  "question": "What is Zeroth?"\n}',
        payloadSource: "user",
      }),
    );
    api.getHealth.mockResolvedValue({
      deployment_ref: "workflow-1-v1",
      graph_version_ref: "workflow-1@1",
    });
    api.getInputContract.mockResolvedValue({
      name: "research-query",
      version: 1,
      json_schema: {
        type: "object",
        required: ["query"],
        properties: { query: { type: "string", minLength: 1 } },
      },
    });
    await mountEditor();

    await act(async () => button("Run")?.click());
    const input = document.querySelector<HTMLTextAreaElement>(
      'textarea[data-evidence-id="studio.run.input-payload"]',
    );
    await waitFor(() => expect(input?.value).toContain('"query": "example"'));
    expect(input?.value).not.toContain('"question"');
  });
});
