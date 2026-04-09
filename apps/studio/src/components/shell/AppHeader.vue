<script setup lang="ts">
import { computed } from 'vue'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(
  defineProps<{
    isDirty?: boolean
    isSaving?: boolean
    version?: number
  }>(),
  {
    isDirty: false,
    isSaving: false,
    version: 1,
  },
)

const ui = useUiStore()

const saveLabel = computed(() => {
  if (props.isSaving) return 'Saving...'
  if (props.isDirty) return 'Unsaved'
  return 'Saved'
})

const saveClass = computed(() => {
  if (props.isSaving) return 'saving'
  if (props.isDirty) return 'dirty'
  return 'clean'
})
</script>

<template>
  <header class="app-header">
    <div class="header-left">
      <span class="header-title">Zeroth Studio</span>
    </div>

    <div class="header-center">
      <div class="mode-switch">
        <button
          class="mode-tab"
          :class="{ active: ui.currentMode === 'editor' }"
          @click="ui.setMode('editor')"
        >
          Editor
        </button>
        <button
          class="mode-tab"
          :class="{ active: ui.currentMode === 'executions' }"
          @click="ui.setMode('executions')"
        >
          Executions
        </button>
      </div>
    </div>

    <div class="header-right">
      <span class="env-label">Env / Dev</span>
      <span class="version-label">Draft v{{ version }}</span>
      <span class="save-indicator" :class="saveClass">
        <span class="save-dot"></span>
        {{ saveLabel }}
      </span>
      <button class="publish-btn" disabled>Publish</button>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 76px;
  padding: 16px 24px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 12px;
  box-shadow:
    0 18px 48px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
}

.header-left {
  display: flex;
  align-items: center;
}

.header-title {
  font-family: var(--font-family-studio);
  font-size: 19px;
  font-weight: 500;
  color: var(--color-studio-text);
}

.header-center {
  position: absolute;
  left: 50%;
  bottom: 0;
  transform: translate(-50%, 50%);
  z-index: 10;
}

.mode-switch {
  display: flex;
  gap: 0;
  background: rgba(255, 255, 255, 0.6);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  padding: 3px;
}

.mode-tab {
  font-family: var(--font-family-studio);
  font-size: 12px;
  font-weight: 500;
  padding: 6px 16px;
  border: none;
  background: transparent;
  color: var(--color-studio-text-secondary);
  cursor: pointer;
  border-radius: 6px;
  transition: all 120ms ease;
}

.mode-tab:hover {
  color: var(--color-studio-text);
}

.mode-tab.active {
  background: rgba(79, 205, 255, 0.18);
  color: var(--color-studio-text);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.env-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-studio-text-secondary);
}

.version-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-studio-text-tertiary);
}

.save-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-studio-text-secondary);
  transition: color 120ms ease;
}

.save-indicator.clean .save-dot {
  background: rgba(72, 199, 142, 0.8);
}

.save-indicator.dirty .save-dot {
  background: rgba(255, 180, 60, 0.8);
}

.save-indicator.saving .save-dot {
  background: rgba(79, 205, 255, 0.8);
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.save-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(72, 199, 142, 0.8);
  transition: background 120ms ease;
}

.publish-btn {
  font-family: var(--font-family-studio);
  font-size: 12px;
  font-weight: 500;
  padding: 6px 16px;
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.4);
  color: var(--color-studio-text-faint);
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
