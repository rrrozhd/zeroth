<script setup lang="ts">
defineProps<{
  collapsed: boolean
}>()

defineEmits<{
  toggle: []
}>()
</script>

<template>
  <aside class="workflow-rail" :class="{ collapsed }">
    <div class="rail-content" v-show="!collapsed">
      <div class="rail-header">
        <span class="rail-eyebrow">WORKFLOWS</span>
        <button class="collapse-btn" @click="$emit('toggle')" title="Collapse sidebar">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M10 4L6 8L10 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>

      <button class="new-project-btn">New Project</button>

      <div class="empty-state">
        <h3 class="empty-heading">No workflows yet</h3>
        <p class="empty-body">
          Create your first workflow to start building governed agent pipelines.
          Click 'New Project' in the sidebar to begin.
        </p>
      </div>
    </div>

    <button
      v-if="collapsed"
      class="expand-btn"
      @click="$emit('toggle')"
      title="Expand sidebar"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M6 4L10 8L6 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  </aside>
</template>

<style scoped>
.workflow-rail {
  width: 304px;
  min-width: 304px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 12px;
  box-shadow:
    0 18px 48px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
  transition: all 120ms ease;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.workflow-rail.collapsed {
  width: 40px;
  min-width: 40px;
  align-items: center;
  justify-content: flex-start;
  padding-top: 12px;
}

.rail-content {
  display: flex;
  flex-direction: column;
  padding: 20px;
  gap: 16px;
  flex: 1;
}

.rail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.rail-eyebrow {
  font-size: 11px;
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.24em;
  color: var(--color-studio-text-faint);
}

.collapse-btn,
.expand-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--color-studio-text-secondary);
  cursor: pointer;
  border-radius: 6px;
  transition: all 120ms ease;
}

.collapse-btn:hover,
.expand-btn:hover {
  background: rgba(79, 205, 255, 0.08);
  color: var(--color-studio-text);
}

.new-project-btn {
  font-family: var(--font-family-studio);
  font-size: 13px;
  font-weight: 500;
  padding: 10px 16px;
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  background: rgba(79, 205, 255, 0.1);
  color: var(--color-studio-text);
  cursor: pointer;
  transition: all 120ms ease;
  width: 100%;
}

.new-project-btn:hover {
  background: rgba(79, 205, 255, 0.18);
}

.empty-state {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 0;
}

.empty-heading {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-studio-text);
}

.empty-body {
  font-size: 13px;
  font-weight: 400;
  line-height: 1.5;
  color: var(--color-studio-text-tertiary);
}
</style>
