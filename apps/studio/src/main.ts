import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { createPinia } from "pinia";
import { computed, defineComponent, h, watch } from "vue";
import { createApp } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";

import { router } from "@/router";
import { useStudioShellStore } from "@/stores/studioShell";

import "@/styles/tokens.css";

const queryClient = new QueryClient();

const ShellApp = defineComponent({
  name: "StudioShellApp",
  setup() {
    const route = useRoute();
    const shellStore = useStudioShellStore();
    const modeTabs = [
      { label: "Editor", name: "editor" as const },
      { label: "Executions", name: "executions" as const },
      { label: "Tests", name: "tests" as const },
    ];

    const activeMode = computed(
      () =>
        (route.name === "editor" ||
        route.name === "executions" ||
        route.name === "tests"
          ? route.name
          : "editor") as "editor" | "executions" | "tests",
    );

    watch(
      activeMode,
      (mode) => {
        shellStore.setCurrentRouteMode(mode);
      },
      { immediate: true },
    );

    return () =>
      h("div", { class: "studio-shell" }, [
        h("header", { class: "studio-header" }, [
          h("div", { class: "studio-header__title" }, [
            h("span", { class: "studio-label" }, "Canvas-first authoring"),
            h("h1", { class: "studio-display" }, "Zeroth Studio"),
          ]),
          h("nav", { "aria-label": "Studio mode switch", class: "mode-switch" }, [
            ...modeTabs.map((tab) =>
              h(
                RouterLink,
                {
                  class: ["mode-switch__link", activeMode.value === tab.name && "is-active"],
                  to: { name: tab.name },
                },
                () => tab.label,
              ),
            ),
          ]),
        ]),
        h("main", { class: "studio-layout" }, [
          h("aside", { class: "studio-rail" }, [
            h("p", { class: "studio-label" }, "Workflows"),
            h("div", { class: "studio-rail__item is-active" }, "Current workspace"),
            h("div", { class: "studio-rail__item" }, "Assets"),
          ]),
          h("section", { class: "studio-canvas" }, [h(RouterView)]),
          h("aside", { class: "studio-inspector" }, [
            h("p", { class: "studio-label" }, "Session state"),
            h("p", { class: "studio-body" }, `Route mode: ${shellStore.currentRouteMode}`),
            h("p", { class: "studio-body" }, `Save status: ${shellStore.saveStatus}`),
          ]),
        ]),
      ]);
  },
});

const app = createApp(ShellApp);

app.use(createPinia());
app.use(router);
app.use(VueQueryPlugin, { queryClient });
app.mount("#app");
