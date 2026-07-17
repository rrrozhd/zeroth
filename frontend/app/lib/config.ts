// Runtime configuration for the Zeroth Console.
//
// Under `output: "export"` nothing is baked at build time, so the API base URL,
// API key, environment label, and tenant are read from localStorage at runtime.
// This lets one identical bundle work in both deploy modes:
//   - Mounted by the Zeroth app  -> base is empty -> requests go same-origin
//     (e.g. `/v1/runs`, `/api/studio/v1/...` on the host serving /console).
//   - Standalone host            -> base is an explicit origin + CORS on the API.

const BASE_KEY = "zeroth.apiBase";
const KEY_KEY = "zeroth.apiKey";
const ENV_KEY = "zeroth.env"; // "local" | "staging" | "production"
const TENANT_KEY = "zeroth.tenant";

/** Read a persisted value. Guards `window` so it is safe during the static
 *  prerender, where localStorage does not exist. */
function read(key: string): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(key);
}

/** Persist a value. No-op during the static prerender. */
function write(key: string, value: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(key, value);
}

export function getApiBase(): string {
  return read(BASE_KEY) ?? "";
}

export function getApiKey(): string {
  return read(KEY_KEY) ?? "";
}

export function setConfig(base: string, key: string): void {
  // Strip trailing slashes so `${base}${path}` never doubles up.
  write(BASE_KEY, base.trim().replace(/\/+$/, ""));
  write(KEY_KEY, key.trim());
}

/** A key is required for every /v1 and /api/studio call; the base is optional
 *  (empty == same-origin, the mounted-mode default). */
export function isConfigured(): boolean {
  return getApiKey().length > 0;
}

/** Deployment-environment label shown in the Topbar (defaults to "local"). */
export function getEnv(): string {
  return read(ENV_KEY) ?? "local";
}

/** Active tenant shown in the Topbar breadcrumb (defaults to "default"). */
export function getTenant(): string {
  return read(TENANT_KEY) ?? "default";
}

/** Persist the env label + tenant together (set from the Connect bar). */
export function setEnvTenant(env: string, tenant: string): void {
  write(ENV_KEY, env);
  write(TENANT_KEY, tenant);
}
