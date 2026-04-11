<script setup lang="ts">
import { inject, computed } from 'vue'

const props = defineProps<{
  selected: boolean
  label: string
  metaLabel: string
  nodeId: string
}>()

const emit = defineEmits<{
  delete: []
}>()

const nodeValidation = inject<{
  isValid: (id: string) => boolean
  getIssues: (id: string) => { type: string; message: string }[]
}>('nodeValidation', { isValid: () => true, getIssues: () => [] })

const hasIssues = computed(() => !nodeValidation.isValid(props.nodeId))
</script>

<template>
  <div
    class="base-node"
    :class="{ 'base-node--selected': selected, 'base-node--invalid': hasIssues }"
  >
    <div class="base-node__toolbar">
      <button
        class="base-node__delete"
        @click.stop="emit('delete')"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
        >
          <path
            d="M3 3.5h8M5.5 3.5V2.5a1 1 0 011-1h1a1 1 0 011 1v1M9.5 6v4.5a1 1 0 01-1 1h-3a1 1 0 01-1-1V6M6 8v2M8 8v2"
            stroke="currentColor"
            stroke-width="1.2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </button>
    </div>
    <div class="base-node__card">
      <div
        v-if="hasIssues"
        class="base-node__warning"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
        >
          <path
            d="M7 1L13 12H1L7 1Z"
            stroke="currentColor"
            stroke-width="1.2"
            stroke-linejoin="round"
          />
          <path
            d="M7 5v3M7 10v.5"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-linecap="round"
          />
        </svg>
      </div>
      <div class="base-node__icon">
        <slot name="icon" />
      </div>
      <div class="base-node__title">
        {{ label }}
      </div>
      <div class="base-node__meta">
        {{ metaLabel }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.base-node {
  position: relative;
  width: 10rem;
}

.base-node__toolbar {
  position: absolute;
  top: -36px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 4px;
  padding: 4px;
  background: rgba(18, 48, 68, 0.85);
  backdrop-filter: blur(8px);
  border-radius: 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 100ms ease;
}

.base-node:hover .base-node__toolbar {
  opacity: 1;
  pointer-events: auto;
}

.base-node__delete {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: rgba(255, 255, 255, 0.7);
  border-radius: 5px;
  cursor: pointer;
  transition: all 120ms ease;
}

.base-node__delete:hover {
  background: rgba(255, 80, 80, 0.2);
  color: rgba(255, 80, 80, 1);
}

.base-node__card {
  width: 10rem;
  padding: 0.8rem 0.9rem;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.38), rgba(236, 250, 255, 0.18));
  border: 1px solid rgba(100, 178, 206, 0.45);
  border-radius: 12px;
  box-shadow: 0 16px 28px rgba(115, 164, 193, 0.06);
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}

.base-node--selected .base-node__card {
  border: 2px solid rgba(79, 205, 255, 1);
  box-shadow:
    0 16px 28px rgba(115, 164, 193, 0.06),
    0 0 0 4px rgba(79, 205, 255, 0.2);
}

.base-node--invalid .base-node__card {
  border-color: rgba(255, 80, 80, 0.7);
}

.base-node--invalid.base-node--selected .base-node__card {
  border-color: rgba(255, 80, 80, 0.9);
  box-shadow:
    0 16px 28px rgba(115, 164, 193, 0.06),
    0 0 0 4px rgba(255, 80, 80, 0.2);
}

.base-node__warning {
  position: absolute;
  top: 6px;
  right: 6px;
  color: rgba(255, 80, 80, 0.8);
}

.base-node__icon {
  width: 28px;
  height: 28px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(79, 180, 220, 0.8);
}

.base-node__title {
  font-size: 1rem;
  font-weight: 500;
  line-height: 1.3;
  color: #123044;
}

.base-node__meta {
  font-size: 0.6rem;
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  line-height: 1.2;
  color: rgba(79, 180, 220, 0.8);
  margin-top: 4px;
}
</style>
