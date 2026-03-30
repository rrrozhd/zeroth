<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { VueFlow, type Edge, type Node } from "@vue-flow/core";
import { Activity, FlaskConical, PlayCircle } from "lucide-vue-next";
import { useQuery } from "@tanstack/vue-query";

import { createStudioApiClient, type WorkflowDetail, type WorkflowGraph } from "@/lib/api/studio";
import { useStudioShellStore, type StudioRouteMode } from "@/stores/studioShell";

const props = defineProps<{
  mode: StudioRouteMode;
}>();

const shellStore = useStudioShellStore();
const studioClient = createStudioApiClient();

const fallbackWorkflow: WorkflowDetail = {
  workflow_id: "local-intake",
  workspace_id: "local",
  name: "Lead intake triage",
  folder_path: "/Acquisition",
  revision_token: "local-revision",
  last_saved_at: new Date().toISOString(),
  graph: {
    graph_id: "local-graph",
    version: 7,
    entry_step: "capture-request",
    nodes: [
      {
        id: "capture-request",
        type: "agent",
        name: "Capture request",
        input_contract_ref: "contract://input",
        output_contract_ref: "contract://risk",
      },
      { id: "risk-screen", type: "agent", name: "Risk screen", output_contract_ref: "contract://risk" },
      { id: "publish-brief", type: "executable_unit", name: "Publish brief", input_contract_ref: "contract://risk" },
    ],
    edges: [
      { source: "capture-request", target: "risk-screen" },
      { source: "risk-screen", target: "publish-brief" },
    ],
  },
};

const draftGraph = ref<WorkflowGraph>(structuredClone(fallbackWorkflow.graph));
const lastSavedSignature = ref("");

const workflowDetailQuery = useQuery({
  queryKey: computed(() => ["studio", "workflow", shellStore.selectedWorkflowId]),
  enabled: computed(() => Boolean(shellStore.selectedWorkflowId)),
  queryFn: async () => {
    if (!shellStore.selectedWorkflowId) {
      return fallbackWorkflow;
    }

    try {
      return await studioClient.getWorkflow(shellStore.selectedWorkflowId);
    } catch {
      return {
        ...fallbackWorkflow,
        workflow_id: shellStore.selectedWorkflowId,
        name: shellStore.selectedWorkflowName || fallbackWorkflow.name,
      };
    }
  },
});

const workflowDetail = computed(() => workflowDetailQuery.data.value ?? fallbackWorkflow);

watch(
  workflowDetail,
  (detail) => {
    draftGraph.value = structuredClone(detail.graph);
    lastSavedSignature.value = JSON.stringify(detail.graph);
    shellStore.setWorkflowDetail(detail);
  },
  { immediate: true },
);

watch(
  draftGraph,
  async (graph) => {
    const nextSignature = JSON.stringify(graph);
    if (nextSignature === lastSavedSignature.value) {
      return;
    }

    shellStore.setSaveStatus("dirty");
    if (props.mode !== "editor" || !shellStore.hasActiveLease) {
      return;
    }

    const saved = await shellStore.saveWorkflowDraft(graph);
    if (saved) {
      lastSavedSignature.value = JSON.stringify(saved.graph);
      draftGraph.value = structuredClone(saved.graph);
    }
  },
  { deep: true },
);

const flowNodes = computed<Node[]>(() => {
  const graphNodes = Array.isArray(draftGraph.value.nodes) ? draftGraph.value.nodes : [];

  return graphNodes.map((graphNode, index) => {
    const record = graphNode as Record<string, unknown>;
    const id = String(record.id ?? `node-${index + 1}`);
    const label = String(record.name ?? record.title ?? record.id ?? `Node ${index + 1}`);
    const type = String(record.type ?? "default");

    return {
      id,
      type: "default",
      position: {
        x: 100 + (index % 3) * 260,
        y: 120 + Math.floor(index / 3) * 180,
      },
      data: {
        label: `${label}\n${type.replaceAll("_", " ")}`,
      },
      class: shellStore.selectedNodeId === id ? "workflow-canvas__node is-selected" : "workflow-canvas__node",
    };
  });
});

const flowEdges = computed<Edge[]>(() => {
  const graphEdges = Array.isArray(draftGraph.value.edges) ? draftGraph.value.edges : [];

  return graphEdges.map((graphEdge, index) => {
    const record = graphEdge as Record<string, unknown>;
    return {
      id: String(record.id ?? `edge-${index + 1}`),
      source: String(record.source ?? ""),
      target: String(record.target ?? ""),
      animated: props.mode === "executions",
    };
  });
});

const modeCopy = computed(() => {
  if (props.mode === "executions") {
    return {
      icon: PlayCircle,
      label: "Executions",
      body: "Run history and approvals stay ambient here until the dedicated runtime views land.",
    };
  }
  if (props.mode === "tests") {
    return {
      icon: FlaskConical,
      label: "Tests",
      body: "Persisted-draft test runs will appear here without displacing the shared shell.",
    };
  }
  return {
    icon: Activity,
    label: "Editor",
    body: shellStore.hasActiveLease
      ? "Structural workflow edits autosave while the current lease stays active."
      : "Acquire a workflow lease before structural edits autosave.",
  };
});

watch(
  flowNodes,
  (nodes) => {
    const selectedNode = nodes.find((node) => node.id === shellStore.selectedNodeId);
    if (!selectedNode && nodes[0]) {
      shellStore.setSelectedNodeId(nodes[0].id);
    }
  },
  { immediate: true },
);

function handleNodeClick(event: { node: Node }): void {
  shellStore.setSelectedNodeId(event.node.id);
}
</script>

<template>
  <section class="workflow-canvas">
    <div class="workflow-canvas__header">
      <div>
        <p class="studio-label">Canvas</p>
        <h2 class="studio-heading">{{ workflowDetail.name }}</h2>
      </div>
      <div class="workflow-canvas__meta">
        <span>Revision {{ workflowDetail.graph.version }}</span>
        <span>{{ flowNodes.length }} nodes</span>
      </div>
    </div>

    <div class="workflow-canvas__viewport">
      <VueFlow
        class="workflow-canvas__flow"
        :nodes="flowNodes"
        :edges="flowEdges"
        fit-view-on-init
        :min-zoom="0.4"
        :max-zoom="1.4"
        @node-click="handleNodeClick"
      />

      <div class="workflow-canvas__mode-card">
        <component :is="modeCopy.icon" :size="18" aria-hidden="true" />
        <div>
          <p class="studio-label">{{ modeCopy.label }}</p>
          <p class="studio-body">{{ modeCopy.body }}</p>
        </div>
      </div>
    </div>
  </section>
</template>
