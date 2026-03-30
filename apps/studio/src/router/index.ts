import { createRouter, createWebHistory } from "vue-router";

import AppShell from "@/app/AppShell.vue";

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
      component: AppShell,
    },
    {
      path: "/executions",
      name: "executions",
      component: AppShell,
    },
    {
      path: "/tests",
      name: "tests",
      component: AppShell,
    },
  ],
});
