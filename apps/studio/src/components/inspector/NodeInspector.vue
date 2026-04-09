<script setup lang="ts">
import { computed } from 'vue'
import { useCanvasStore } from '../../stores/canvas'
import { useUiStore } from '../../stores/ui'
import { NODE_TYPE_REGISTRY } from '../../types/nodes'
import { useNodeValidation } from '../../composables/useNodeValidation'
import InspectorField from './InspectorField.vue'

const canvasStore = useCanvasStore()
const uiStore = useUiStore()

const selectedNode = computed(() =>
  canvasStore.nodes.find(n => n.id === uiStore.selectedNodeId)
)
const typeDef = computed(() =>
  selectedNode.value ? NODE_TYPE_REGISTRY[selectedNode.value.type] : null
)
const properties = computed(() => typeDef.value?.properties ?? [])

const { getIssues } = useNodeValidation(
  () => canvasStore.nodes,
  () => canvasStore.edges
)

const currentIssues = computed(() =>
  selectedNode.value ? getIssues(selectedNode.value.id) : []
)

function updateProperty(key: string, value: unknown) {
  if (!selectedNode.value) return
  const oldValue = selectedNode.value.data[key]
  canvasStore.updateNodePropertyWithUndo(selectedNode.value.id, key, oldValue, value)
}
</script>

<template>
  <aside class="node-inspector" v-if="selectedNode && typeDef">
    <div class="inspector-header">
      <span class="inspector-eyebrow">INSPECTOR</span>
      <span class="inspector-type">{{ typeDef.label }}</span>
    </div>
    <div class="inspector-body">
      <div v-if="currentIssues.length > 0" class="inspector-validation">
        <div class="inspector-validation__header">Validation Issues</div>
        <div v-for="(issue, i) in currentIssues" :key="i" class="inspector-validation__item">
          <svg width="12" height="12" viewBox="0 0 14 14" fill="none" class="inspector-validation__icon">
            <path d="M7 1L13 12H1L7 1Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
            <path d="M7 5v3M7 10v.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
          <span>{{ issue.message }}</span>
        </div>
      </div>
      <InspectorField
        v-for="prop in properties"
        :key="prop.key"
        :definition="prop"
        :model-value="selectedNode.data[prop.key] ?? prop.default ?? ''"
        @update:model-value="updateProperty(prop.key, $event)"
      />
    </div>
  </aside>
</template>

<style scoped>
.node-inspector {
  width: 280px;
  min-width: 280px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 12px;
  box-shadow:
    0 18px 48px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.inspector-header {
  padding: 20px 20px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.inspector-eyebrow {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.24em;
  color: var(--color-studio-text-secondary, #5a7a8a);
  font-weight: 600;
}

.inspector-type {
  font-size: 16px;
  font-weight: 600;
  color: #123044;
}

.inspector-body {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.inspector-validation {
  background: rgba(255, 80, 80, 0.06);
  border: 1px solid rgba(255, 80, 80, 0.15);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 8px;
}

.inspector-validation__header {
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  color: rgba(220, 60, 60, 0.9);
  margin-bottom: 6px;
}

.inspector-validation__item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: rgba(220, 60, 60, 0.8);
}

.inspector-validation__icon {
  flex-shrink: 0;
}
</style>
