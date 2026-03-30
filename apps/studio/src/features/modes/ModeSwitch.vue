<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

const route = useRoute();

const modes = [
  { label: "Editor", name: "editor" as const },
  { label: "Executions", name: "executions" as const },
  { label: "Tests", name: "tests" as const },
];

const activeMode = computed(() => {
  if (route.name === "executions" || route.name === "tests") {
    return route.name;
  }
  return "editor";
});
</script>

<template>
  <nav class="mode-switch" aria-label="Studio mode switch">
    <RouterLink
      v-for="mode in modes"
      :key="mode.name"
      :to="{ name: mode.name }"
      class="mode-switch__link"
      :class="{ 'is-active': activeMode === mode.name }"
    >
      {{ mode.label }}
    </RouterLink>
  </nav>
</template>
