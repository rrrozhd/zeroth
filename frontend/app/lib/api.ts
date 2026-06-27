// Minimal typed API client for the Zeroth Console.
//
// NOTE: the WorkflowSummary type below is hand-written for the M1 slice. The
// plumbing milestone replaces these with types generated from the app's
// /openapi.json (openapi-typescript) so they cannot drift from the backend.

import { getApiBase, getApiKey } from "./config";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = getApiBase();
  const key = getApiKey();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (key) headers.set("X-API-Key", key);

  let res: Response;
  try {
    res = await fetch(`${base}${path}`, { ...init, headers });
  } catch (e) {
    const where = base || "this origin";
    throw new ApiError(0, `Network error reaching ${where}${path}: ${(e as Error).message}`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? JSON.stringify(body);
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- Studio ----

export interface WorkflowSummary {
  id: string;
  name: string;
  version: number;
  status: string;
  updated_at: string;
}

export function listWorkflows(): Promise<WorkflowSummary[]> {
  return apiFetch<WorkflowSummary[]>("/api/studio/v1/workflows");
}
