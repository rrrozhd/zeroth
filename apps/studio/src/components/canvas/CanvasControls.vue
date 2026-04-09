<script setup lang="ts">
import { ref } from 'vue'
import { useVueFlow } from '@vue-flow/core'
import { useCanvasActions } from '../../composables/useCanvasActions'
import { NODE_TYPE_REGISTRY } from '../../types/nodes'

const { zoomIn, zoomOut } = useVueFlow()
const { addNodeAtCenter, fitToView } = useCanvasActions()

const showNodeMenu = ref(false)

const nodeTypes = Object.entries(NODE_TYPE_REGISTRY).map(([key, def]) => ({
  key,
  label: def.label,
}))

function handleAddNode(type: string) {
  addNodeAtCenter(type)
  showNodeMenu.value = false
}
</script>

<template>
  <div class="canvas-controls">
    <button class="canvas-controls__btn" title="Fit to view" @click="fitToView()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
    <button class="canvas-controls__btn" title="Zoom in" @click="zoomIn()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M7 3v8M3 7h8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
      </svg>
    </button>
    <button class="canvas-controls__btn" title="Zoom out" @click="zoomOut()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M3 7h8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
      </svg>
    </button>
    <button class="canvas-controls__btn" title="Tidy up" disabled>
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <rect x="1" y="1" width="5" height="5" rx="1" stroke="currentColor" stroke-width="1.2"/>
        <rect x="8" y="8" width="5" height="5" rx="1" stroke="currentColor" stroke-width="1.2"/>
        <path d="M3.5 6v2.5H6M10.5 8V5.5H8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
    <div class="canvas-controls__divider" />
    <div class="canvas-controls__add-wrapper">
      <button class="canvas-controls__btn" title="Add node" @click="showNodeMenu = !showNodeMenu">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <rect x="1" y="1" width="12" height="12" rx="2" stroke="currentColor" stroke-width="1.2"/>
          <path d="M7 4v6M4 7h6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        </svg>
      </button>
      <div v-if="showNodeMenu" class="canvas-controls__menu">
        <button
          v-for="nt in nodeTypes"
          :key="nt.key"
          class="canvas-controls__menu-item"
          @click="handleAddNode(nt.key)"
        >
          {{ nt.label }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.canvas-controls {
  position: absolute;
  bottom: 16px;
  left: 16px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  box-shadow:
    0 8px 24px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
  z-index: 10;
}

.canvas-controls__btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: #123044;
  border-radius: 5px;
  cursor: pointer;
  transition: all 120ms ease;
}

.canvas-controls__btn:hover:not(:disabled) {
  background: rgba(79, 205, 255, 0.08);
  color: #0a1f2e;
}

.canvas-controls__btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.canvas-controls__divider {
  width: 1px;
  height: 20px;
  background: rgba(118, 182, 205, 0.3);
  margin: 0 2px;
}

.canvas-controls__add-wrapper {
  position: relative;
}

.canvas-controls__menu {
  position: absolute;
  bottom: 36px;
  left: 0;
  min-width: 160px;
  padding: 4px;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  box-shadow: 0 12px 32px rgba(117, 160, 189, 0.12);
  display: flex;
  flex-direction: column;
  gap: 2px;
  z-index: 20;
}

.canvas-controls__menu-item {
  display: block;
  width: 100%;
  padding: 6px 10px;
  border: none;
  background: transparent;
  color: #123044;
  font-size: 13px;
  text-align: left;
  border-radius: 5px;
  cursor: pointer;
  transition: background 120ms ease;
}

.canvas-controls__menu-item:hover {
  background: rgba(79, 205, 255, 0.08);
}
</style>
