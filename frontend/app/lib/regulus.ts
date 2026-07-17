import { getApiBase } from "./config";

export type RegulusStatus = "enabled" | "absent" | "unknown";

export function regulusStatusFrom(httpStatus: number): RegulusStatus {
  if (httpStatus === 404) return "absent"; // mount guarded off (no regulus extra)
  return "enabled"; // 200/401/403 => sub-app is mounted
}

/** Probe the mounted Regulus openapi once. Cheap, unauthenticated GET. */
export async function detectRegulus(): Promise<RegulusStatus> {
  try {
    const res = await fetch(`${getApiBase()}/regulus/openapi.json`, { method: "GET" });
    return regulusStatusFrom(res.status);
  } catch {
    return "unknown";
  }
}
