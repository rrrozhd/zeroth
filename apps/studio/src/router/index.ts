import { computed, defineComponent, h } from "vue";
import { createRouter, createWebHistory, RouterView, useRoute } from "vue-router";

const routeMeta = {
  editor: {
    label: "Editor",
    description: "Canvas-first workflow authoring route skeleton.",
  },
  executions: {
    label: "Executions",
    description: "Run timelines and governance surfaces attach here in later plans.",
  },
  tests: {
    label: "Tests",
    description: "Workflow test harnesses target the persisted draft snapshot from here.",
  },
} as const;

const RouteSkeletonView = defineComponent({
  name: "RouteSkeletonView",
  setup() {
    const route = useRoute();
    const content = computed(() => routeMeta[(route.name ?? "editor") as keyof typeof routeMeta]);

    return () =>
      h("section", { class: "route-skeleton" }, [
        h("p", { class: "studio-label" }, "Studio route"),
        h("h2", { class: "studio-heading" }, content.value.label),
        h("p", { class: "studio-body" }, content.value.description),
        h("div", { class: "route-skeleton__viewport" }, [
          h("div", { class: "route-skeleton__canvas" }, "Canvas and shell composition land here."),
          h("div", { class: "route-skeleton__context" }, "Contextual runtime and inspector state stay progressive."),
        ]),
        h(RouterView),
      ]);
  },
});

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      redirect: { name: "editor" },
    },
    {
      path: "/editor",
      name: "editor",
      component: RouteSkeletonView,
    },
    {
      path: "/executions",
      name: "executions",
      component: RouteSkeletonView,
    },
    {
      path: "/tests",
      name: "tests",
      component: RouteSkeletonView,
    },
  ],
});
