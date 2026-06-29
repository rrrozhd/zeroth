// Runtime configuration for the Zeroth Console.
//
// Under `output: "export"` nothing is baked at build time, so the API base URL
// and API key are read from localStorage at runtime. This lets one identical
// bundle work in both deploy modes:
//   - Mounted by the Zeroth app  -> base is empty -> requests go same-origin
//     (e.g. `/v1/runs`, `/api/studio/v1/...` on the host serving /console).
//   - Standalone host            -> base is an explicit origin + CORS on the API.

const BASE_KEY = "zeroth.apiBase";
const KEY_KEY = "zeroth.apiKey";

export function getApiBase(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(BASE_KEY) ?? "";
}

export function getApiKey(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(KEY_KEY) ?? "";
}

export function setConfig(base: string, key: string): void {
  if (typeof window === "undefined") return;
  // Strip trailing slashes so `${base}${path}` never doubles up.
  window.localStorage.setItem(BASE_KEY, base.trim().replace(/\/+$/, ""));
  window.localStorage.setItem(KEY_KEY, key.trim());
}

/** A key is required for every /v1 and /api/studio call; the base is optional
 *  (empty == same-origin, the mounted-mode default). */
export function isConfigured(): boolean {
  return getApiKey().length > 0;
}
