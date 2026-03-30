import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { createPinia } from "pinia";
import { defineComponent, h } from "vue";
import { createApp } from "vue";
import { RouterView } from "vue-router";

import { router } from "@/router";

import "@/styles/tokens.css";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";

const queryClient = new QueryClient();
const RootApp = defineComponent({
  name: "StudioRootApp",
  setup() {
    return () => h(RouterView);
  },
});

const app = createApp(RootApp);

app.use(createPinia());
app.use(router);
app.use(VueQueryPlugin, { queryClient });
app.mount("#app");
