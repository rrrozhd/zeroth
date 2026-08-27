import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

// Node ≥22 ships an experimental `localStorage` global that is `undefined`
// unless --localstorage-file is passed; it shadows jsdom's implementation and
// breaks every test that touches window.localStorage. The config runs in the
// main process before workers spawn; forked workers inherit NODE_OPTIONS and
// re-parse it at spawn (worker threads would not — hence pool: "forks").
const NO_WEBSTORAGE = "--no-experimental-webstorage";
if (!(process.env.NODE_OPTIONS ?? "").includes(NO_WEBSTORAGE)) {
  process.env.NODE_OPTIONS = [process.env.NODE_OPTIONS, NO_WEBSTORAGE]
    .filter(Boolean)
    .join(" ");
}

export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  test: {
    include: ["app/**/*.test.ts", "app/**/*.test.tsx"],
    environment: "node",
    // Forked processes pick up the NODE_OPTIONS set above; worker threads
    // cannot drop a node flag after process start.
    pool: "forks",
  },
});
