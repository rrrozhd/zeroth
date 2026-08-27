import { getApiBase, getApiKey } from "./config";
import { getIdentity } from "./api";

export type RegulusStatus = "enabled" | "absent" | "unknown";

/** Classify an *authenticated* probe of a Regulus route.
 *
 * The whole Zeroth service sits behind API-key auth middleware, so an
 * unauthenticated request to any path — mounted or not — returns 401. The probe
 * therefore MUST carry the key; only then does the status distinguish routing:
 *   - 2xx  → the /regulus sub-app is mounted and served it → enabled
 *   - 404  → auth passed but nothing is mounted at /regulus → absent (no extra)
 *   - else → 401/403 (missing/invalid key/role) → can't tell → unknown (hide) */
export function regulusStatusFrom(httpStatus: number): RegulusStatus {
  if (httpStatus >= 200 && httpStatus < 300) return "enabled";
  // The console reaches Regulus only through the admin-gated proxy:
  //   403 → caller lacks the admin role   (hide the section)
  //   503 → Regulus mount is disabled     (hide)
  //   404 → proxy/route absent             (hide)
  if (httpStatus === 403 || httpStatus === 503 || httpStatus === 404) return "absent";
  return "unknown";
}

/** Probe the admin-gated Regulus proxy once, WITH the API key. Resolve identity
 *  first so built-in read-only roles do not generate a predictably forbidden
 *  403 on every console route. The Regulus nav section shows only on a definite
 *  "enabled"; unavailable identity fails closed without probing the proxy. */
export async function detectRegulus(): Promise<RegulusStatus> {
  const key = getApiKey();
  if (!key) return "unknown";
  try {
    const identity = await getIdentity();
    const predictablyReadOnly = identity.roles.length > 0 && identity.roles.every(
      (role) => role === "operator" || role === "reviewer",
    );
    if (predictablyReadOnly) {
      return "absent";
    }
  } catch {
    return "unknown";
  }
  try {
    const res = await fetch(`${getApiBase()}/v1/econ/regulus/dashboard/kpis`, {
      method: "GET",
      headers: { "X-API-Key": key },
    });
    return regulusStatusFrom(res.status);
  } catch {
    return "unknown";
  }
}
