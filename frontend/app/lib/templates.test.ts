import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  createDeployment: vi.fn(),
  createContract: vi.fn(),
  createWorkflow: vi.fn(),
  listDeployments: vi.fn(),
  listContracts: vi.fn(),
  listWorkflows: vi.fn(),
  publishWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
}));

vi.mock("@/app/lib/api", () => api);

import { instantiateTemplate, WORKFLOW_TEMPLATES } from "./templates";

beforeEach(() => {
  vi.clearAllMocks();
  api.listContracts.mockResolvedValue([]);
  api.listDeployments.mockResolvedValue([]);
  api.listWorkflows.mockResolvedValue([]);
  api.createContract.mockResolvedValue({});
  api.createWorkflow.mockResolvedValue({ id: "workflow-1" });
  api.updateWorkflow.mockResolvedValue({ id: "workflow-1" });
  api.publishWorkflow.mockResolvedValue({ id: "workflow-1", version: 1 });
  api.createDeployment.mockResolvedValue({});
});

describe("workflow templates", () => {
  it("owns every contract referenced by every node", () => {
    for (const template of WORKFLOW_TEMPLATES) {
      const names = new Set(template.contracts.map((contract) => contract.name));
      for (const node of template.nodes) {
        expect(names.has(String(node.data!.input_contract_ref))).toBe(true);
        expect(names.has(String(node.data!.output_contract_ref))).toBe(true);
      }
    }
  });

  it("uses self-contained inline executable units rather than missing manifests", () => {
    for (const template of WORKFLOW_TEMPLATES) {
      for (const node of template.nodes.filter((candidate) => candidate.type === "code")) {
        expect(node.data!.config).toMatchObject({ execution_mode: "inline" });
        expect(String((node.data!.config as Record<string, unknown>).inline_source)).not.toBe("");
      }
      expect(template.nodes.some((node) => node.type === "executable_unit")).toBe(false);
    }
  });

  it("covers loops, batches, subgraphs, joins, agents, retrieval, approval, and code", () => {
    const nodes = WORKFLOW_TEMPLATES.flatMap((template) => template.nodes);
    const edges = WORKFLOW_TEMPLATES.flatMap((template) => template.edges);
    const types = new Set(nodes.map((candidate) => candidate.type));

    expect(WORKFLOW_TEMPLATES).toHaveLength(5);
    expect(WORKFLOW_TEMPLATES.every((template) => template.nodes.length >= 4)).toBe(true);
    for (const type of ["agent", "retrieval", "code", "subgraph", "human_approval", "loop"]) {
      expect(types.has(type)).toBe(true);
    }
    expect(nodes.some((candidate) => candidate.data?.parallel_config)).toBe(true);
    expect(nodes.some((candidate) => candidate.data?.join_config)).toBe(true);
    expect(edges.some((candidate) => candidate.condition?.allow_cycle_traversal)).toBe(true);
  });

  it("ships two provider-free bounded loop demos with explicit safety settings", () => {
    const demos = WORKFLOW_TEMPLATES.filter((template) =>
      ["data-quality-repair-loop", "incident-readiness-loop"].includes(template.id),
    );

    expect(demos).toHaveLength(2);
    for (const template of demos) {
      expect(template.nodes.every((candidate) => candidate.type !== "agent")).toBe(true);
      expect(template.nodes.every((candidate) => candidate.type !== "retrieval")).toBe(true);
      expect(template.edges.some((candidate) => candidate.condition?.allow_cycle_traversal)).toBe(true);
      const loop = template.nodes.find((candidate) => candidate.type === "loop");
      expect(loop?.data?.config).toMatchObject({
        until: expect.any(String),
        max_retries: expect.any(Number),
      });
      expect(
        template.edges.filter((candidate) => candidate.source === loop?.id).map((edge) => edge.source_handle),
      ).toEqual(expect.arrayContaining(["repeat", "done", "limit"]));
      expect(
        template.edges.find(
          (candidate) => candidate.source === loop?.id && candidate.source_handle === "repeat",
        )?.condition,
      ).toMatchObject({ allow_cycle_traversal: true });
      expect(
        template.edges.some(
          (candidate) =>
            candidate.target === loop?.id && candidate.condition?.allow_cycle_traversal === true,
        ),
      ).toBe(true);
      expect(template.execution_settings).toMatchObject({
        max_total_steps: expect.any(Number),
        max_total_runtime_seconds: expect.any(Number),
        max_visits_per_node: expect.any(Number),
        max_visits_per_edge: expect.any(Number),
      });
      const maxRetries = Number((loop?.data?.config as Record<string, unknown>)?.max_retries);
      expect(template.execution_settings!.max_visits_per_node).toBeGreaterThanOrEqual(maxRetries + 2);
      expect(template.execution_settings!.max_visits_per_edge).toBeGreaterThanOrEqual(maxRetries + 1);
    }
  });

  it("gives the incident demo a controlled unresolved-field path to exercise Limit", () => {
    const template = WORKFLOW_TEMPLATES.find((item) => item.id === "incident-readiness-loop")!;
    const prepare = template.nodes.find((node) => node.id === "prepare")!;

    expect(String((prepare.data!.config as Record<string, unknown>).inline_source)).toContain(
      "blocked_fields",
    );
  });

  it("registers missing template contracts before saving the graph", async () => {
    const template = WORKFLOW_TEMPLATES[0];

    await instantiateTemplate(template);

    expect(api.createContract).toHaveBeenCalledTimes(template.contracts.length);
    expect(api.updateWorkflow).toHaveBeenCalledWith(
      "workflow-1",
      expect.objectContaining({
        entry_step: "start",
        nodes: template.nodes,
        edges: template.edges,
        execution_settings: template.execution_settings,
      }),
    );
    expect(api.createContract.mock.invocationCallOrder.at(-1)).toBeLessThan(
      api.createWorkflow.mock.invocationCallOrder[0],
    );
  });

  it("publishes and deploys a missing subgraph dependency before the parent draft", async () => {
    const template = WORKFLOW_TEMPLATES.find((item) => item.id === "batch-investigation")!;
    let workflow = 0;
    api.createWorkflow.mockImplementation(async () => ({ id: `workflow-${++workflow}` }));
    api.updateWorkflow.mockImplementation(async (id: string) => ({ id, version: 1 }));
    api.publishWorkflow.mockImplementation(async (id: string) => ({ id, version: 1 }));

    await instantiateTemplate(template);

    expect(api.publishWorkflow).toHaveBeenCalledWith("workflow-1");
    expect(api.createDeployment).toHaveBeenCalledWith({
      deployment_ref: "template-iterative-research",
      graph_id: "workflow-1",
      graph_version: 1,
    });
    expect(api.createDeployment.mock.invocationCallOrder[0]).toBeLessThan(
      api.createWorkflow.mock.invocationCallOrder.at(-1)!,
    );
  });
});
