<script setup lang="ts">
import { onMounted } from 'vue'
import { useWorkflowStore } from '../../stores/workflow'
import { useWorkflowPersistence } from '../../composables/useWorkflowPersistence'
import { useUiStore } from '../../stores/ui'
import NodePalette from '../palette/NodePalette.vue'

defineProps<{
  collapsed: boolean
}>()

defineEmits<{
  toggle: []
}>()

const workflowStore = useWorkflowStore()
const persistence = useWorkflowPersistence()
const uiStore = useUiStore()

onMounted(() => {
  workflowStore.fetchWorkflows()
})

async function handleNewProject() {
  const name = window.prompt('Workflow name:', 'Untitled Workflow')
  if (!name) return
  const result = await workflowStore.createNew(name)
  if (result) {
    await persistence.loadWorkflow(result.id)
  }
}

async function handleSelectWorkflow(id: string) {
  await persistence.loadWorkflow(id)
}
</script>

<template>
  <aside
    class="workflow-rail"
    :class="{ collapsed }"
  >
    <div
      v-show="!collapsed"
      class="rail-content"
    >
      <div class="rail-header">
        <span class="rail-eyebrow">{{ uiStore.currentMode === 'editor' && workflowStore.currentWorkflowId ? 'NODE PALETTE' : 'WORKFLOWS' }}</span>
        <button
          class="collapse-btn"
          title="Collapse sidebar"
          @click="$emit('toggle')"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M10 4L6 8L10 12"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </button>
      </div>

      <NodePalette v-if="uiStore.currentMode === 'editor' && workflowStore.currentWorkflowId" />

      <template v-else>
        <button
          class="new-project-btn"
          @click="handleNewProject"
        >
          New Project
        </button>

        <div
          v-if="workflowStore.workflows.length > 0"
          class="workflow-list"
        >
          <button
            v-for="workflow in workflowStore.workflows"
            :key="workflow.id"
            class="workflow-item"
            :class="{ active: workflowStore.currentWorkflowId === workflow.id }"
            @click="handleSelectWorkflow(workflow.id)"
          >
            <span class="workflow-name">{{ workflow.name }}</span>
            <span class="workflow-meta">Draft v{{ workflow.version }}</span>
          </button>
        </div>

        <div
          v-else
          class="empty-state"
        >
          <h3 class="empty-heading">
            No workflows yet
          </h3>
          <p class="empty-body">
            Create your first workflow to start building governed agent pipelines.
            Click 'New Project' in the sidebar to begin.
          </p>
        </div>

        <div
          v-if="workflowStore.error"
          class="error-banner"
        >
          {{ workflowStore.error }}
        </div>
      </template>
    </div>

    <button
      v-if="collapsed"
      class="expand-btn"
      title="Expand sidebar"
      @click="$emit('toggle')"
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
      >
        <path
          d="M6 4L10 8L6 12"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>
  </aside>
</template>

<style scoped>
.workflow-rail {
  width: 304px;
  min-width: 304px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 12px;
  box-shadow:
    0 18px 48px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
  transition: all 120ms ease;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.workflow-rail.collapsed {
  width: 40px;
  min-width: 40px;
  align-items: center;
  justify-content: flex-start;
  padding-top: 12px;
}

.rail-content {
  display: flex;
  flex-direction: column;
  padding: 20px;
  gap: 16px;
  flex: 1;
  overflow-y: auto;
}

.rail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.rail-eyebrow {
  font-size: 11px;
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.24em;
  color: var(--color-studio-text-faint);
}

.collapse-btn,
.expand-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--color-studio-text-secondary);
  cursor: pointer;
  border-radius: 6px;
  transition: all 120ms ease;
}

.collapse-btn:hover,
.expand-btn:hover {
  background: rgba(79, 205, 255, 0.08);
  color: var(--color-studio-text);
}

.new-project-btn {
  font-family: var(--font-family-studio);
  font-size: 13px;
  font-weight: 500;
  padding: 10px 16px;
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  background: rgba(79, 205, 255, 0.1);
  color: var(--color-studio-text);
  cursor: pointer;
  transition: all 120ms ease;
  width: 100%;
}

.new-project-btn:hover {
  background: rgba(79, 205, 255, 0.18);
}

.workflow-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.workflow-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: none;
  background: transparent;
  text-align: left;
  cursor: pointer;
  border-radius: 8px;
  border-left: 3px solid transparent;
  transition: all 120ms ease;
}

.workflow-item:hover {
  background: rgba(79, 205, 255, 0.08);
}

.workflow-item.active {
  border-left-color: rgba(79, 205, 255, 0.7);
  background: rgba(79, 205, 255, 0.06);
}

.workflow-name {
  font-size: 14px;
  font-weight: 400;
  color: var(--color-studio-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workflow-meta {
  font-size: 12px;
  font-weight: 400;
  color: var(--color-studio-text-tertiary);
}

.empty-state {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 0;
}

.empty-heading {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-studio-text);
}

.empty-body {
  font-size: 13px;
  font-weight: 400;
  line-height: 1.5;
  color: var(--color-studio-text-tertiary);
}

.error-banner {
  font-size: 12px;
  font-weight: 400;
  line-height: 1.4;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255, 80, 80, 0.08);
  border: 1px solid rgba(255, 80, 80, 0.2);
  color: rgba(220, 60, 60, 0.9);
  margin-top: auto;
}
</style>
