<script setup lang="ts">
import StudioCanvas from '../canvas/StudioCanvas.vue'
</script>

<template>
  <main class="canvas-area">
    <StudioCanvas />
  </main>
</template>

<style scoped>
.canvas-area {
  flex: 1;
  min-width: 0;
  min-height: 550px;
  background: rgba(255, 255, 255, 0.28);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 12px;
  box-shadow:
    0 18px 48px rgba(117, 160, 189, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.6);
  position: relative;
  overflow: hidden;
}
</style>
