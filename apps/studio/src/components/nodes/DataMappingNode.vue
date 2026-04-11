<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import BaseNode from './BaseNode.vue'

defineProps<NodeProps>()
</script>

<template>
  <BaseNode
    :selected="selected"
    :label="data.label"
    meta-label="DATA MAPPING"
    :node-id="id"
  >
    <template #icon>
      <svg
        width="20"
        height="20"
        viewBox="0 0 20 20"
        fill="none"
      >
        <path
          d="M3 6h4M3 10h4M3 14h4M13 6h4M13 10h4M13 14h4"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linecap="round"
        />
        <path
          d="M7 6l6 4M7 10l6 0M7 14l6-4"
          stroke="currentColor"
          stroke-width="1.2"
          stroke-linecap="round"
        />
      </svg>
    </template>
    <Handle
      id="input-data"
      type="target"
      :position="Position.Left"
      class="studio-handle"
    />
    <Handle
      id="output-data"
      type="source"
      :position="Position.Right"
      class="studio-handle"
    />
  </BaseNode>
</template>

<style scoped>
.studio-handle {
  width: 10px;
  height: 10px;
  border: 1.5px solid rgba(79, 180, 220, 0.7);
  background: rgba(255, 255, 255, 0.9);
  transition: transform 150ms ease, border-color 150ms ease;
}
.studio-handle:hover {
  transform: scale(1.5);
  border-color: rgba(79, 205, 255, 1);
}
</style>
