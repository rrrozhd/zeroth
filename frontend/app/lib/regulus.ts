import { getApiBase, getApiKey } from "./config";

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

/** Probe the admin-gated Regulus proxy once, WITH the API key. The Regulus nav
 *  section shows only on a definite "enabled" (200 = mounted AND caller is a
 *  platform admin); 403/503/404/network all resolve to hidden. */
export async function detectRegulus(): Promise<RegulusStatus> {
  const key = getApiKey();
  if (!key) return "unknown";
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
