<script setup lang="ts">
import { ref, computed } from 'vue'
import { NODE_TYPE_REGISTRY } from '../../types/nodes'
import PaletteItem from './PaletteItem.vue'

const props = defineProps<{
  label: string
  nodeTypes: string[]
  searchFilter: string
}>()

const expanded = ref(true)

const filteredTypes = computed(() => {
  if (!props.searchFilter) return props.nodeTypes
  const query = props.searchFilter.toLowerCase()
  return props.nodeTypes.filter((type) => {
    const def = NODE_TYPE_REGISTRY[type]
    return def && def.label.toLowerCase().includes(query)
  })
})

function toggle() {
  expanded.value = !expanded.value
}
</script>

<template>
  <div
    v-if="filteredTypes.length > 0"
    class="palette-category"
  >
    <button
      class="category-header"
      @click="toggle"
    >
      <span class="category-label">{{ label }}</span>
      <svg
        class="category-chevron"
        :class="{ collapsed: !expanded }"
        width="12"
        height="12"
        viewBox="0 0 12 12"
        fill="none"
      >
        <path
          d="M3 4.5L6 7.5L9 4.5"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>
    <div
      v-show="expanded"
      class="category-body"
    >
      <PaletteItem
        v-for="type in filteredTypes"
        :key="type"
        :node-type="type"
        :label="NODE_TYPE_REGISTRY[type]?.label ?? type"
        :icon="NODE_TYPE_REGISTRY[type]?.icon ?? '?'"
      />
    </div>
  </div>
</template>

<style scoped>
.palette-category {
  display: flex;
  flex-direction: column;
}

.category-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 0;
  border: none;
  background: transparent;
  cursor: pointer;
  width: 100%;
}

.category-label {
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: var(--color-studio-text-faint);
}

.category-chevron {
  color: var(--color-studio-text-faint);
  transition: transform 120ms ease;
}

.category-chevron.collapsed {
  transform: rotate(-90deg);
}

.category-body {
  display: flex;
  flex-direction: column;
  margin-top: 4px;
  gap: 2px;
}
</style>
