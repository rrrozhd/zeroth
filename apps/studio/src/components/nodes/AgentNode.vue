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
    meta-label="AGENT"
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
          d="M10 2l1.5 3.5L15 7l-3.5 1.5L10 12l-1.5-3.5L5 7l3.5-1.5L10 2z"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linejoin="round"
        />
        <path
          d="M15 12l.8 1.8L18 15l-2.2.8L15 18l-.8-2.2L12 15l2.2-.8L15 12z"
          stroke="currentColor"
          stroke-width="1"
          stroke-linejoin="round"
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
