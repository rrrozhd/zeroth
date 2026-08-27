// Built-in workflow templates: ready-made example graphs the user can
// instantiate instead of starting from a blank canvas. Node and edge shapes
// mirror the Studio API schemas, and configs pre-fill the exact keys the
// NodeInspector edits, so a template opens fully editable and nearly
// publish-ready (only environment-specific refs need adjusting).

import {
  createDeployment,
  createContract,
  createWorkflow,
  listDeployments,
  listContracts,
  listWorkflows,
  publishWorkflow,
  updateWorkflow,
  type StudioEdgeInput,
  type StudioNode,
  type WorkflowDetail,
} from "@/app/lib/api";

export type WorkflowTemplate = {
  id: string;
  name: string;
  tagline: string;
  description: string;
  contracts: {
    name: string;
    json_schema: Record<string, unknown>;
    metadata: Record<string, unknown>;
  }[];
  depends_on?: string[];
  deployment_ref?: string;
  nodes: StudioNode[];
  edges: StudioEdgeInput[];
  execution_settings?: WorkflowDetail["execution_settings"];
};

// Every node type exposes the same single data-in / data-out port pair
// (see studio_api._io_ports on the backend).
function edge(
  source: string,
  target: string,
  condition?: StudioEdgeInput["condition"],
  sourceHandle = "output-data",
  targetHandle = "input-data",
): StudioEdgeInput {
  return {
    id: `e-${source}.${sourceHandle}-${target}.${targetHandle}`,
    source,
    target,
    source_handle: sourceHandle,
    target_handle: targetHandle,
    kind: "data",
    enabled: true,
    ...(condition ? { condition } : {}),
  };
}

function node(
  id: string,
  type: string,
  label: string,
  x: number,
  config: Record<string, unknown>,
  inputContractRef?: string,
  outputContractRef?: string,
  execution?: Partial<
    Pick<NonNullable<StudioNode["data"]>, "parallel_config" | "join_config">
  >,
  y = 120,
): StudioNode {
  return {
    id,
    type,
    position: { x, y },
    data: {
      label,
      config,
      input_contract_ref: inputContractRef ?? null,
      output_contract_ref: outputContractRef ?? null,
      ...execution,
    },
  };
}

function loopRouteEdge(source: string, target: string, route: "repeat" | "done" | "limit") {
  return edge(
    source,
    target,
    {
      expression: `payload.zeroth_loop['${source}'].route == '${route}'`,
      operand_refs: [`payload.zeroth_loop.${source}.route`],
      branch_rule: "expression",
      allow_cycle_traversal: route === "repeat",
      metadata: { loop_route: route },
    },
    route,
  );
}

const ENVELOPE_SCHEMA = {
  type: "object",
  properties: {
    query: { type: "string" },
    question: { type: "string" },
    items: { type: "array", items: {} },
    iteration: { type: "integer", minimum: 0 },
    needs_revision: { type: "boolean" },
    retrieved: { type: "array", items: { type: "object" } },
    analyses: { type: "array", items: {} },
  },
  additionalProperties: true,
};

function contract(name: string, json_schema: Record<string, unknown>) {
  return { name, json_schema, metadata: { owner: "zeroth-studio-template" } };
}

export const WORKFLOW_TEMPLATES: WorkflowTemplate[] = [
  {
    id: "iterative-research",
    name: "Iterative evidence research",
    tagline: "Retrieval → Agents → Bounded loop",
    description:
      "A deployable research unit that retrieves evidence, critiques gaps, iterates at most twice, and returns a grounded synthesis.",
    deployment_ref: "template-iterative-research",
    contracts: [
      contract("template.iterative-research.envelope", ENVELOPE_SCHEMA),
    ],
    nodes: [
      node("start", "entrypoint", "Research request", -420, {}, "template.iterative-research.envelope", "template.iterative-research.envelope"),
      node("retrieve", "retrieval", "Retrieve evidence", -140, {
        connector_ref: "key_value", query_key: "query", top_k: 8, as_name: "retrieved",
      }, "template.iterative-research.envelope", "template.iterative-research.envelope"),
      node("research-agent", "agent", "Grounded researcher", 140, {
        instruction: "Use only retrieved evidence. Preserve the envelope, add findings and citations, and identify evidence gaps.",
        model_provider: "openai/gpt-4o-mini",
        memory_refs: ["key_value"], timeout_seconds: 30, criticality: "medium",
      }, "template.iterative-research.envelope", "template.iterative-research.envelope"),
      node("critique", "agent", "Evidence critic", 420, {
        instruction: "Critique unsupported claims. Preserve the envelope and set needs_revision; do not invent sources.",
        model_provider: "openai/gpt-4o-mini", timeout_seconds: 30, criticality: "high",
      }, "template.iterative-research.envelope", "template.iterative-research.envelope"),
      node("advance", "code", "Bound iteration", 700, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["iteration"] = int(data.get("iteration", 0)) + 1\ndata["needs_revision"] = bool(data.get("needs_revision", False)) and data["iteration"] < 2\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.iterative-research.envelope", "template.iterative-research.envelope"),
      node("finalize", "agent", "Final grounded answer", 980, {
        instruction: "Return the final answer with citations and a concise uncertainty note. Preserve trace identifiers.",
        model_provider: "openai/gpt-4o-mini", timeout_seconds: 30, criticality: "high",
      }, "template.iterative-research.envelope", "template.iterative-research.envelope"),
    ],
    edges: [
      edge("start", "retrieve"), edge("retrieve", "research-agent"),
      edge("research-agent", "critique"), edge("critique", "advance"),
      edge("advance", "retrieve", {
        expression: "payload.needs_revision == True",
        operand_refs: ["payload.needs_revision"], branch_rule: "expression",
        allow_cycle_traversal: true, metadata: { max_iterations: 2 },
      }),
      edge("advance", "finalize", {
        expression: "payload.needs_revision != True",
        operand_refs: ["payload.needs_revision"], branch_rule: "expression",
        allow_cycle_traversal: false, metadata: {},
      }),
    ],
  },
  {
    id: "batch-investigation",
    name: "Batched investigation orchestrator",
    tagline: "Batch fan-out → Subgraph → Join",
    description:
      "Normalizes a case batch, runs every case through the deployed research subgraph with bounded concurrency, then synthesizes the collected results.",
    depends_on: ["iterative-research"],
    contracts: [
      contract("template.batch-investigation.envelope", ENVELOPE_SCHEMA),
    ],
    nodes: [
      node("start", "entrypoint", "Case batch", -420, {}, "template.batch-investigation.envelope", "template.batch-investigation.envelope"),
      node("prepare-batch", "code", "Normalize and fan out", -140, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["items"] = list(data.get("items", []))[:24]\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.batch-investigation.envelope", "template.batch-investigation.envelope", {
        parallel_config: {
          split_path: "items", merge_strategy: "collect", reducer_ref: null,
          fail_mode: "best_effort", max_branches: 24, max_concurrency: 4,
          batch_size: 4, branch_timeout_seconds: 45,
        },
      }),
      node("research-subgraph", "subgraph", "Research each case", 140, {
        graph_ref: "template-iterative-research", thread_participation: "isolated", max_depth: 2,
      }, "template.batch-investigation.envelope", "template.batch-investigation.envelope"),
      node("synthesize", "agent", "Synthesize batch", 420, {
        instruction: "Compare all collected case results, preserve dissent and failures, and produce prioritized findings.",
        model_provider: "openai/gpt-4o-mini",
        timeout_seconds: 45, criticality: "high",
      }, "template.batch-investigation.envelope", "template.batch-investigation.envelope"),
    ],
    edges: [edge("start", "prepare-batch"), edge("prepare-batch", "research-subgraph"), edge("research-subgraph", "synthesize")],
  },
  {
    id: "governed-remediation",
    name: "Governed remediation council",
    tagline: "Retrieval → Parallel agents → Approval → Action",
    description:
      "Runs risk and economics reviews in parallel, joins their evidence, requires human approval, executes a bounded action unit, and verifies the result.",
    contracts: [
      contract("template.governed-remediation.envelope", ENVELOPE_SCHEMA),
    ],
    nodes: [
      node("start", "entrypoint", "Remediation request", -560, {}, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("retrieve-policy", "retrieval", "Retrieve policy and history", -320, {
        connector_ref: "key_value", query_key: "query", top_k: 10, as_name: "retrieved",
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("risk-review", "agent", "Risk review", -60, {
        instruction: "Assess safety, reversibility, and policy constraints. Preserve the input envelope.",
        model_provider: "openai/gpt-4o-mini", criticality: "high", timeout_seconds: 30,
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("economics-review", "agent", "Economics review", -60, {
        instruction: "Estimate direct cost, avoided loss, rollback cost, and uncertainty. Preserve the input envelope.",
        model_provider: "openai/gpt-4o-mini", criticality: "medium", timeout_seconds: 30,
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("council", "agent", "Join council evidence", 220, {
        instruction: "Reconcile the collected analyses into one explicit proposed action and rollback plan.",
        model_provider: "openai/gpt-4o-mini", criticality: "high", timeout_seconds: 30,
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope", {
        join_config: { merge_strategy: "collect", reducer_ref: null, merge_path: "analyses" },
      }),
      node("approval", "human_approval", "Human authorization", 500, {
        sla_timeout_seconds: 3600, escalation_action: "reject",
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("apply", "code", "Execute approved unit", 780, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["action_status"] = "simulated_applied"\ndata["rollback_available"] = True\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 10,
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
      node("verify", "agent", "Verify outcome", 1060, {
        instruction: "Verify the action result against the approved proposal and report any residual risk.",
        model_provider: "openai/gpt-4o-mini", criticality: "high", timeout_seconds: 30,
      }, "template.governed-remediation.envelope", "template.governed-remediation.envelope"),
    ],
    edges: [
      edge("start", "retrieve-policy"),
      edge("retrieve-policy", "risk-review"), edge("retrieve-policy", "economics-review"),
      edge("risk-review", "council"), edge("economics-review", "council"),
      edge("council", "approval"), edge("approval", "apply"), edge("apply", "verify"),
    ],
  },
  {
    id: "data-quality-repair-loop",
    name: "Customer data quality repair",
    tagline: "Inspect → Repair → Recheck",
    description:
      "A provider-free operations demo that normalizes customer records, rechecks the result, and exits through explicit bounded loop conditions.",
    contracts: [
      contract("template.data-quality-repair.envelope", ENVELOPE_SCHEMA),
    ],
    execution_settings: {
      max_total_steps: 16,
      max_total_runtime_seconds: 30,
      max_visits_per_node: 4,
      max_visits_per_edge: 3,
      default_timeout_seconds: 5,
    },
    nodes: [
      node("start", "entrypoint", "Customer records", -500, {}, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope"),
      node("retry-quality", "loop", "Repair until clean", -180, {
        until: "payload.needs_repair != True",
        max_retries: 2,
      }, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope"),
      node("inspect", "code", "Inspect quality", 120, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\nrecords = list(data.get("records", []))\nissues = []\nfor index, record in enumerate(records):\n    name = str(record.get("name", ""))\n    email = str(record.get("email", ""))\n    status = str(record.get("status", ""))\n    if name != name.strip() or not name.strip(): issues.append({"row": index, "field": "name"})\n    if email != email.strip().lower() or "@" not in email: issues.append({"row": index, "field": "email"})\n    if status not in {"active", "inactive", "pending"}: issues.append({"row": index, "field": "status"})\ndata["quality_issues"] = issues\ndata["needs_repair"] = bool(issues)\ndata["quality_score"] = 1.0 if not records else round(1 - len(issues) / (len(records) * 3), 3)\ndata.setdefault("repair_pass", 0)\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope", undefined, 20),
      node("repair", "code", "Repair records", 420, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ncleaned = []\nfor record in list(data.get("records", [])):\n    item = dict(record)\n    item["name"] = str(item.get("name", "")).strip() or "Unknown customer"\n    item["email"] = str(item.get("email", "")).strip().lower()\n    if "@" not in item["email"]: item["email"] = "unknown@example.invalid"\n    if item.get("status") not in {"active", "inactive", "pending"}: item["status"] = "pending"\n    cleaned.append(item)\ndata["records"] = cleaned\ndata["repair_pass"] = int(data.get("repair_pass", 0)) + 1\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope", undefined, 20),
      node("finalize", "code", "Quality report", 120, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["result"] = {"status": "ready" if not data.get("needs_repair") else "manual_review", "quality_score": data.get("quality_score"), "repair_passes": data.get("repair_pass", 0), "remaining_issues": len(data.get("quality_issues", []))}\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope", undefined, 280),
      node("manual-review", "code", "Retry limit report", 420, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["result"] = {"status": "manual_review", "reason": "max_retries_exhausted", "repair_passes": data.get("repair_pass", 0), "remaining_issues": len(data.get("quality_issues", []))}\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.data-quality-repair.envelope", "template.data-quality-repair.envelope", undefined, 280),
    ],
    edges: [
      edge("start", "retry-quality"),
      loopRouteEdge("retry-quality", "inspect", "repeat"),
      edge("inspect", "repair"),
      edge("repair", "retry-quality", {
        expression: "True", operand_refs: [], branch_rule: "expression",
        allow_cycle_traversal: true, metadata: { purpose: "recheck_after_repair" },
      }),
      loopRouteEdge("retry-quality", "finalize", "done"),
      loopRouteEdge("retry-quality", "manual-review", "limit"),
    ],
  },
  {
    id: "incident-readiness-loop",
    name: "Incident readiness review",
    tagline: "Assess → Prepare → Reassess",
    description:
      "A provider-free incident-operations demo that fills a safe response brief, reassesses readiness, and records why its loop stopped.",
    contracts: [
      contract("template.incident-readiness.envelope", ENVELOPE_SCHEMA),
    ],
    execution_settings: {
      max_total_steps: 16,
      max_total_runtime_seconds: 30,
      max_visits_per_node: 4,
      max_visits_per_edge: 3,
      default_timeout_seconds: 5,
    },
    nodes: [
      node("start", "entrypoint", "Incident signal", -500, {}, "template.incident-readiness.envelope", "template.incident-readiness.envelope"),
      node("retry-readiness", "loop", "Prepare until ready", -180, {
        until: "payload.ready == True",
        max_retries: 2,
      }, "template.incident-readiness.envelope", "template.incident-readiness.envelope"),
      node("assess", "code", "Assess readiness", 120, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\nrequired = ["service", "severity", "owner", "rollback_plan", "communications"]\nmissing = [key for key in required if not data.get(key)]\ndata["missing_readiness_fields"] = missing\ndata["ready"] = not missing\ndata["readiness_score"] = round((len(required) - len(missing)) / len(required), 2)\ndata.setdefault("planning_round", 0)\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.incident-readiness.envelope", "template.incident-readiness.envelope", undefined, 20),
      node("prepare", "code", "Prepare response brief", 420, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\nblocked_fields = set(data.get("blocked_fields", []))\ndefaults = {"service": "checkout-api", "severity": "SEV-2", "owner": "incident-commander", "rollback_plan": "Restore the last known-good deployment and verify error rate", "communications": "Post updates every 15 minutes in the incident channel"}\nfor key, value in defaults.items():\n    if key not in blocked_fields: data.setdefault(key, value)\ndata["planning_round"] = int(data.get("planning_round", 0)) + 1\ndata["planned_actions"] = ["freeze deploys", "capture baseline", "prepare rollback", "notify stakeholders"]\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.incident-readiness.envelope", "template.incident-readiness.envelope", undefined, 20),
      node("finalize", "code", "Readiness brief", 120, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["result"] = {"status": "ready" if data.get("ready") else "escalate", "score": data.get("readiness_score"), "planning_rounds": data.get("planning_round", 0), "missing": data.get("missing_readiness_fields", [])}\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.incident-readiness.envelope", "template.incident-readiness.envelope", undefined, 280),
      node("escalate", "code", "Retry limit escalation", 420, {
        inline_source: 'import json, sys\ndata = json.load(sys.stdin)\ndata["result"] = {"status": "escalate", "reason": "max_retries_exhausted", "planning_rounds": data.get("planning_round", 0), "missing": data.get("missing_readiness_fields", [])}\njson.dump(data, sys.stdout)\n',
        execution_mode: "inline", timeout_seconds: 5,
      }, "template.incident-readiness.envelope", "template.incident-readiness.envelope", undefined, 280),
    ],
    edges: [
      edge("start", "retry-readiness"),
      loopRouteEdge("retry-readiness", "assess", "repeat"),
      edge("assess", "prepare"),
      edge("prepare", "retry-readiness", {
        expression: "True", operand_refs: [], branch_rule: "expression",
        allow_cycle_traversal: true, metadata: { purpose: "reassess_response_brief" },
      }),
      loopRouteEdge("retry-readiness", "finalize", "done"),
      loopRouteEdge("retry-readiness", "escalate", "limit"),
    ],
  },
];

async function registerTemplateContracts(t: WorkflowTemplate): Promise<void> {
  const existing = new Set((await listContracts()).map((item) => item.name));
  await Promise.all(
    t.contracts
      .filter((item) => !existing.has(item.name))
      .map((item) => createContract(item)),
  );
}

async function createTemplateDraft(t: WorkflowTemplate): Promise<WorkflowDetail> {
  await registerTemplateContracts(t);
  const created = await createWorkflow(t.name);
  return updateWorkflow(created.id, {
    entry_step: "start",
    nodes: t.nodes,
    edges: t.edges,
    viewport: { x: 0, y: 0, zoom: 1 },
    execution_settings: t.execution_settings,
  });
}

async function ensureRunnableDependency(templateId: string): Promise<void> {
  const dependency = WORKFLOW_TEMPLATES.find((candidate) => candidate.id === templateId);
  if (!dependency?.deployment_ref) {
    throw new Error(`Template dependency ${templateId} has no deployment reference`);
  }
  for (const nested of dependency.depends_on ?? []) {
    await ensureRunnableDependency(nested);
  }

  const deployments = await listDeployments();
  if (deployments.some((item) => item.deployment_ref === dependency.deployment_ref)) return;

  const workflows = await listWorkflows();
  let published = workflows.find(
    (item) => item.name === dependency.name && item.status === "published",
  );
  if (!published) {
    const draft = await createTemplateDraft(dependency);
    published = await publishWorkflow(draft.id);
  }
  await createDeployment({
    deployment_ref: dependency.deployment_ref,
    graph_id: published.id,
    graph_version: published.version,
  });
}

/** Create a new draft workflow pre-populated from a template. */
export async function instantiateTemplate(t: WorkflowTemplate): Promise<WorkflowDetail> {
  for (const dependency of t.depends_on ?? []) {
    await ensureRunnableDependency(dependency);
  }
  return createTemplateDraft(t);
}
