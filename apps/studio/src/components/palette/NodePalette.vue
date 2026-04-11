<script setup lang="ts">
import { ref } from 'vue'
import { PALETTE_CATEGORIES } from '../../types/nodes'
import PaletteCategory from './PaletteCategory.vue'

const searchQuery = ref('')
</script>

<template>
  <div class="node-palette">
    <span class="palette-eyebrow">NODE PALETTE</span>

    <input
      v-model="searchQuery"
      class="palette-search"
      type="text"
      placeholder="Search nodes..."
    >

    <div class="palette-categories">
      <PaletteCategory
        v-for="(category, key) in PALETTE_CATEGORIES"
        :key="key"
        :label="category.label"
        :node-types="category.nodeTypes"
        :search-filter="searchQuery"
      />
    </div>
  </div>
</template>

<style scoped>
.node-palette {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow-y: auto;
}

.palette-eyebrow {
  font-size: 11px;
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.24em;
  color: var(--color-studio-text-faint);
}

.palette-search {
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  font-family: var(--font-family-studio);
  background: rgba(255, 255, 255, 0.5);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  color: var(--color-studio-text);
  outline: none;
  transition: border-color 120ms ease;
}

.palette-search::placeholder {
  color: var(--color-studio-text-faint);
}

.palette-search:focus {
  border-color: rgba(79, 205, 255, 0.5);
}

.palette-categories {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
</style>
