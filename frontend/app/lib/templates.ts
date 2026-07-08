// Built-in workflow templates: ready-made example graphs the user can
// instantiate instead of starting from a blank canvas. Node and edge shapes
// mirror the Studio API schemas, and configs pre-fill the exact keys the
// NodeInspector edits, so a template opens fully editable and nearly
// publish-ready (only environment-specific refs need adjusting).

import {
  createWorkflow,
  updateWorkflow,
  type StudioEdge,
  type StudioNode,
  type WorkflowDetail,
} from "@/app/lib/api";

export type WorkflowTemplate = {
  id: string;
  name: string;
  tagline: string;
  description: string;
  nodes: StudioNode[];
  edges: StudioEdge[];
};

// Every node type exposes the same single data-in / data-out port pair
// (see studio_api._io_ports on the backend).
function edge(source: string, target: string): StudioEdge {
  return {
    id: `e-${source}.output-data-${target}.input-data`,
    source,
    target,
    source_handle: "output-data",
    target_handle: "input-data",
    kind: "data",
  };
}

function node(
  id: string,
  type: string,
  label: string,
  x: number,
  config: Record<string, unknown>,
): StudioNode {
  return { id, type, position: { x, y: 120 }, data: { label, config } };
}

export const WORKFLOW_TEMPLATES: WorkflowTemplate[] = [
  {
    id: "rag-qa",
    name: "Grounded Q&A (RAG)",
    tagline: "Retrieval → Agent",
    description:
      "Answer questions using your own knowledge: a retrieval step fetches relevant context, then an agent answers strictly from it.",
    nodes: [
      node("start", "entrypoint", "Start", -280, {}),
      // key_value is one of the three connectors every deployment registers,
      // so the template resolves out of the box; swap it for a vector backend
      // (pgvector/chroma/elasticsearch) in the node editor when configured.
      node("retrieval-context", "retrieval", "Fetch context", 0, {
        connector_ref: "key_value",
        top_k: 5,
      }),
      node("agent-answer", "agent", "Answer with context", 280, {
        instruction:
          "Answer the user's question using only the retrieved context. Cite the passages you relied on, and say explicitly when the context is insufficient instead of guessing.",
        model_provider: "openai/gpt-4o",
      }),
    ],
    edges: [edge("start", "retrieval-context"), edge("retrieval-context", "agent-answer")],
  },
  {
    id: "approval-gate",
    name: "Approval-gated action",
    tagline: "Agent → Human Approval → Executable Unit",
    description:
      "An agent drafts an action, a human signs it off in the Approvals page, and only then does the tool execute it. The core governance pattern.",
    nodes: [
      node("start", "entrypoint", "Start", -280, {}),
      node("agent-draft", "agent", "Draft action", 0, {
        instruction:
          "Draft the action to take for the incoming request. Output a short human-readable summary followed by the exact payload you propose to execute.",
        model_provider: "openai/gpt-4o",
      }),
      node("approval-signoff", "human_approval", "Human sign-off", 280, {
        sla_timeout_seconds: 86400,
      }),
      node("exec-apply", "executable_unit", "Apply action", 560, {
        manifest_ref: "tools/apply_action",
        execution_mode: "native",
      }),
    ],
    edges: [
      edge("start", "agent-draft"),
      edge("agent-draft", "approval-signoff"),
      edge("approval-signoff", "exec-apply"),
    ],
  },
  {
    id: "tool-pipeline",
    name: "Tool → Agent pipeline",
    tagline: "Executable Unit → Agent",
    description:
      "Deterministic code prepares the data (an API call, a transform), then an agent analyzes the result and writes the report.",
    nodes: [
      node("start", "entrypoint", "Start", -280, {}),
      node("exec-prepare", "executable_unit", "Prepare data", 0, {
        manifest_ref: "tools/prepare_input",
        execution_mode: "native",
      }),
      node("agent-analyze", "agent", "Analyze & report", 280, {
        instruction:
          "Analyze the prepared data and produce a concise report: key findings first, then recommended next steps.",
        model_provider: "openai/gpt-4o",
      }),
    ],
    edges: [edge("start", "exec-prepare"), edge("exec-prepare", "agent-analyze")],
  },
];

/** Create a new draft workflow pre-populated from a template. */
export async function instantiateTemplate(t: WorkflowTemplate): Promise<WorkflowDetail> {
  const created = await createWorkflow(t.name);
  return updateWorkflow(created.id, {
    nodes: t.nodes,
    edges: t.edges,
    viewport: { x: 0, y: 0, zoom: 1 },
  });
}
