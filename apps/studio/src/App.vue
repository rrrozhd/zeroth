<script setup lang="ts">
import { onMounted, onUnmounted, watch } from 'vue'
import AppHeader from './components/shell/AppHeader.vue'
import WorkflowRail from './components/shell/WorkflowRail.vue'
import CanvasArea from './components/shell/CanvasArea.vue'
import { useUiStore } from './stores/ui'
import { useCanvasStore } from './stores/canvas'
import { useWorkflowStore } from './stores/workflow'
import { useWorkflowPersistence } from './composables/useWorkflowPersistence'

const ui = useUiStore()
const canvasStore = useCanvasStore()
const workflowStore = useWorkflowStore()
const persistence = useWorkflowPersistence()

// Default viewport getter (canvas plan will override with real Vue Flow viewport)
function getViewport() {
  return { x: 0, y: 0, zoom: 1 }
}

// Mark workflow dirty when canvas changes
watch(
  () => [...canvasStore.nodes],
  () => {
    if (workflowStore.hasWorkflow) {
      workflowStore.markDirty()
    }
  },
  { deep: true },
)

watch(
  () => [...canvasStore.edges],
  () => {
    if (workflowStore.hasWorkflow) {
      workflowStore.markDirty()
    }
  },
  { deep: true },
)

// Ctrl+S / Cmd+S to save
function handleKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault()
    if (workflowStore.hasWorkflow && workflowStore.isDirty) {
      persistence.saveWorkflow(getViewport)
    }
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <div class="shell">
    <AppHeader
      :is-dirty="workflowStore.isDirty"
      :is-saving="workflowStore.isSaving"
      :version="workflowStore.currentVersion"
    />
    <div class="shell-body">
      <WorkflowRail
        :collapsed="ui.railCollapsed"
        @toggle="ui.toggleRail()"
      />
      <CanvasArea />
    </div>
  </div>
</template>

<style scoped>
.shell {
  min-width: 900px;
  min-height: 100vh;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: linear-gradient(180deg, #f7fbff 0%, #eef7fb 100%);
}

.shell-body {
  display: flex;
  flex-direction: row;
  gap: 16px;
  flex: 1;
  min-height: 0;
}
</style>
