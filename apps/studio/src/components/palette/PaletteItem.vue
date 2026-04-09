<script setup lang="ts">
const props = defineProps<{
  nodeType: string
  label: string
  icon: string
}>()

function onDragStart(event: DragEvent) {
  if (!event.dataTransfer) return
  event.dataTransfer.setData('application/vueflow', props.nodeType)
  event.dataTransfer.effectAllowed = 'move'
}
</script>

<template>
  <div
    class="palette-item"
    draggable="true"
    @dragstart="onDragStart"
  >
    <span class="palette-item-icon">{{ icon }}</span>
    <span class="palette-item-label">{{ label }}</span>
  </div>
</template>

<style scoped>
.palette-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 40px;
  padding: 8px 12px;
  border-radius: 8px;
  cursor: grab;
  transition: background 120ms ease;
  user-select: none;
}

.palette-item:hover {
  background: rgba(79, 205, 255, 0.08);
}

.palette-item:active {
  cursor: grabbing;
}

.palette-item-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 4px;
  background: rgba(79, 205, 255, 0.15);
  font-size: 10px;
  color: var(--color-studio-text-secondary);
  flex-shrink: 0;
}

.palette-item-label {
  font-size: 13px;
  font-weight: 400;
  color: #123044;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
