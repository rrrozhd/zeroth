<script setup lang="ts">
import { computed, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { Boxes, FolderTree, Workflow } from "lucide-vue-next";

import { createStudioApiClient, type WorkflowSummary } from "@/lib/api/studio";
import { useStudioShellStore } from "@/stores/studioShell";

const shellStore = useStudioShellStore();
const studioClient = createStudioApiClient();

const fallbackWorkflows: WorkflowSummary[] = [
  {
    workflow_id: "local-intake",
    workspace_id: "local",
    name: "Lead intake triage",
    folder_path: "/Acquisition",
    draft_graph_version: 7,
    validation_status: "valid",
  },
  {
    workflow_id: "local-review",
    workspace_id: "local",
    name: "Governance review lane",
    folder_path: "/Governance",
    draft_graph_version: 3,
    validation_status: "unknown",
  },
  {
    workflow_id: "local-tests",
    workspace_id: "local",
    name: "Regression harness",
    folder_path: "/Quality",
    draft_graph_version: 2,
    validation_status: "invalid",
  },
];

const workflowQuery = useQuery({
  queryKey: ["studio", "workflows"],
  queryFn: async () => {
    try {
      return await studioClient.listWorkflows();
    } catch {
      return fallbackWorkflows;
    }
  },
});

const workflows = computed(() => workflowQuery.data.value ?? fallbackWorkflows);

const folderGroups = computed(() => {
  const groups = new Map<string, WorkflowSummary[]>();
  for (const workflow of workflows.value) {
    const folder = workflow.folder_path || "/";
    const folderEntries = groups.get(folder) ?? [];
    folderEntries.push(workflow);
    groups.set(folder, folderEntries);
  }

  return Array.from(groups.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([folder, items]) => ({
      folder,
      items: items.slice().sort((left, right) => left.name.localeCompare(right.name)),
    }));
});

watch(
  workflows,
  (items) => {
    if (items.length === 0) {
      shellStore.setSelectedWorkflowId(null);
      shellStore.setSelectedWorkflowName(null);
      return;
    }

    const selectedWorkflow = items.find((item) => item.workflow_id === shellStore.selectedWorkflowId);
    const nextWorkflow = selectedWorkflow ?? items[0];

    void shellStore.openWorkflow({
      workflowId: nextWorkflow.workflow_id,
      workflowName: nextWorkflow.name,
    });
  },
  { immediate: true },
);

function selectWorkflow(workflow: WorkflowSummary): void {
  void shellStore.openWorkflow({
    workflowId: workflow.workflow_id,
    workflowName: workflow.name,
  });
}

function workflowStatusLabel(status: WorkflowSummary["validation_status"]): string {
  if (status === "valid") {
    return "Ready";
  }
  if (status === "invalid") {
    return "Needs fixes";
  }
  return "Draft";
}
</script>

<template>
  <aside class="workflow-rail" aria-label="Workflow rail">
    <div class="workflow-rail__header">
      <div>
        <p class="studio-label">Workflows</p>
        <h2 class="studio-heading">Authoring space</h2>
      </div>
      <FolderTree :size="18" aria-hidden="true" />
    </div>

    <div class="workflow-rail__tree">
      <section v-for="group in folderGroups" :key="group.folder" class="workflow-rail__group">
        <p class="workflow-rail__folder">{{ group.folder }}</p>
        <button
          v-for="workflow in group.items"
          :key="workflow.workflow_id"
          type="button"
          class="workflow-rail__row"
          :class="{ 'is-active': shellStore.selectedWorkflowId === workflow.workflow_id }"
          @click="selectWorkflow(workflow)"
        >
          <span class="workflow-rail__row-main">
            <Workflow :size="15" aria-hidden="true" />
            <span>{{ workflow.name }}</span>
          </span>
          <span class="workflow-rail__row-status">{{ workflowStatusLabel(workflow.validation_status) }}</span>
        </button>
      </section>
    </div>

    <button type="button" class="workflow-rail__assets">
      <span class="workflow-rail__row-main">
        <Boxes :size="15" aria-hidden="true" />
        <span>Assets</span>
      </span>
      <span class="workflow-rail__assets-note">Reusable building blocks</span>
    </button>
  </aside>
</template>
