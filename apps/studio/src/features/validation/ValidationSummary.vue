<script setup lang="ts">
import { computed } from "vue";
import { AlertCircle, AlertTriangle } from "lucide-vue-next";

import type { GraphValidationIssue, GraphValidationReport } from "@/lib/api/validation";

const props = defineProps<{
  report: GraphValidationReport | null;
  nodeId?: string | null;
}>();

const issues = computed(() => {
  if (!props.report) {
    return [] as GraphValidationIssue[];
  }
  if (!props.nodeId) {
    return props.report.issues;
  }
  return props.report.issues.filter((issue) => issue.node_id === props.nodeId);
});

const summary = computed(() => ({
  errors: issues.value.filter((issue) => issue.severity === "error"),
  warnings: issues.value.filter((issue) => issue.severity === "warning"),
}));

const groupedIssues = computed(() => {
  const groups = new Map<string, GraphValidationIssue[]>();
  for (const issue of issues.value) {
    const key = issue.edge_id ? "Edge issues" : issue.node_id ? "Node issues" : "Graph issues";
    const group = groups.get(key) ?? [];
    group.push(issue);
    groups.set(key, group);
  }
  return Array.from(groups.entries()).map(([label, entries]) => ({ label, entries }));
});
</script>

<template>
  <section class="validation-summary">
    <div class="validation-summary__header">
      <div>
        <p class="studio-label">Validation</p>
        <h3 class="studio-heading">Local feedback</h3>
      </div>
      <div class="validation-summary__counts">
        <span class="validation-summary__count is-error">{{ summary.errors.length }} errors</span>
        <span class="validation-summary__count is-warning">{{ summary.warnings.length }} warnings</span>
      </div>
    </div>

    <p v-if="issues.length === 0" class="studio-body">
      Validation issues for this node will appear here after the current draft is checked.
    </p>

    <div v-for="group in groupedIssues" :key="group.label" class="validation-summary__group">
      <p class="validation-summary__group-label">{{ group.label }}</p>
      <ul class="validation-summary__issues">
        <li v-for="issue in group.entries" :key="`${issue.code}-${issue.message}`" class="validation-summary__issue">
          <component
            :is="issue.severity === 'error' ? AlertCircle : AlertTriangle"
            :size="16"
            aria-hidden="true"
          />
          <div>
            <strong>{{ issue.code }}</strong>
            <p class="studio-body">{{ issue.message }}</p>
          </div>
        </li>
      </ul>
    </div>
  </section>
</template>
