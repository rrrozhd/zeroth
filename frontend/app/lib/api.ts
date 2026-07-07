// Typed API client for the Zeroth Console.
//
// Types come from app/lib/api-types.ts, generated from the platform's
// /openapi.json via `npm run gen:api` (openapi-typescript), so request/response
// shapes cannot drift from the backend. The thin apiFetch wrapper adds the
// runtime API base + X-API-Key from lib/config.ts.

import type { components } from "./api-types";
import { getApiBase, getApiKey } from "./config";

type S = components["schemas"];

export type HealthResponse = S["HealthResponse"];
export type RunStatus = S["RunStatusResponse"];
export type RunInvocationResponse = S["RunInvocationResponse"];
export type AdminRunList = S["AdminRunListResponse"];
export type ApprovalRecord = S["ApprovalRecord"];
export type ApprovalResolutionRequest = S["ApprovalResolutionRequest"];
export type ApprovalResolutionResponse = S["ApprovalResolutionResponse"];
export type AuditRecordList = S["AuditRecordListResponse"];
export type AuditTimeline = S["AuditTimelineResponse"];
export type NodeAuditRecord = S["NodeAuditRecord"];
export type DeploymentCost = S["DeploymentCostResponse"];
export type DeploymentSummary = S["DeploymentSummaryResponse"];
export type ConnectorSummary = S["ConnectorSummaryResponse"];
export type ConnectorCreateRequest = S["ConnectorCreateRequest"];
export type ConnectorUpdateRequest = S["ConnectorUpdateRequest"];
export type ConnectorTestResponse = S["ConnectorTestResponse"];
export type ManifestSummary = S["ManifestSummaryResponse"];
export type WorkflowSummary = S["WorkflowSummaryResponse"];
export type WorkflowDetail = S["WorkflowDetailResponse"];
export type UpdateWorkflowRequest = S["UpdateWorkflowRequest"];
export type NodeType = S["NodeTypeResponse"];
export type StudioNode = S["StudioNodeResponse"];
export type StudioEdge = S["StudioEdgeResponse"];
export type StudioViewport = S["StudioViewport"];

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

// ---- Health (also used to discover the deployment_ref) ----

export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}

let _refCache: { base: string; ref: string } | null = null;

/** The deployment ref for the connected API; cached per API base. */
export async function deploymentRef(): Promise<string> {
  const base = getApiBase();
  if (_refCache && _refCache.base === base) return _refCache.ref;
  const health = await getHealth();
  _refCache = { base, ref: health.deployment_ref };
  return health.deployment_ref;
}

// ---- Runs ----

export function listRuns(): Promise<AdminRunList> {
  return apiFetch<AdminRunList>("/v1/admin/runs");
}

export function getRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/v1/runs/${encodeURIComponent(runId)}`);
}

export function submitRun(body: {
  input_payload?: Record<string, unknown>;
  thread_id?: string | null;
}): Promise<RunInvocationResponse> {
  return apiFetch<RunInvocationResponse>("/v1/runs", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Ordered per-node audit timeline for one run. */
export function getRunTimeline(runId: string): Promise<AuditTimeline> {
  return apiFetch<AuditTimeline>(`/v1/runs/${encodeURIComponent(runId)}/timeline`);
}

// ---- Approvals (deployment-scoped) ----

export async function listApprovals(): Promise<ApprovalRecord[]> {
  const ref = await deploymentRef();
  return apiFetch<ApprovalRecord[]>(`/v1/deployments/${encodeURIComponent(ref)}/approvals`);
}

export async function resolveApproval(
  approvalId: string,
  body: ApprovalResolutionRequest,
): Promise<ApprovalResolutionResponse> {
  const ref = await deploymentRef();
  return apiFetch<ApprovalResolutionResponse>(
    `/v1/deployments/${encodeURIComponent(ref)}/approvals/${encodeURIComponent(approvalId)}/resolve`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---- Audit (deployment-scoped) ----

export async function listAudits(): Promise<AuditRecordList> {
  const ref = await deploymentRef();
  return apiFetch<AuditRecordList>(`/v1/deployments/${encodeURIComponent(ref)}/audits`);
}

/** Audit records for one graph node, newest first. The endpoint filters by
    `node_id` but has no limit param, so the cap is applied client-side
    (records come back oldest-first — created_at order). */
export async function listNodeAudits(
  nodeId: string,
  limit = 20,
): Promise<NodeAuditRecord[]> {
  const ref = await deploymentRef();
  const res = await apiFetch<AuditRecordList>(
    `/v1/deployments/${encodeURIComponent(ref)}/audits?node_id=${encodeURIComponent(nodeId)}`,
  );
  return (res.records ?? []).slice(-limit).reverse();
}

// ---- Deployments ----

/** All persisted deployment versions; `serving` marks this service's own. */
export function listDeployments(): Promise<DeploymentSummary[]> {
  return apiFetch<DeploymentSummary[]>("/v1/deployments");
}

// ---- Memory connectors ----

/** Registered memory connectors — the resolvable connector_ref values. */
export function listConnectors(): Promise<ConnectorSummary[]> {
  return apiFetch<ConnectorSummary[]>("/v1/connectors");
}

/** Create a runtime-managed connector (requires CONNECTOR_ADMIN). */
export function createConnector(body: ConnectorCreateRequest): Promise<ConnectorSummary> {
  return apiFetch<ConnectorSummary>("/v1/connectors", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Reconfigure an existing runtime-managed connector (rebuild + re-register). */
export function updateConnector(
  ref: string,
  body: ConnectorUpdateRequest,
): Promise<ConnectorSummary> {
  return apiFetch<ConnectorSummary>(`/v1/connectors/${encodeURIComponent(ref)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** Delete a runtime-managed connector (env-sourced ones 409). */
export function deleteConnector(ref: string): Promise<void> {
  return apiFetch<void>(`/v1/connectors/${encodeURIComponent(ref)}`, {
    method: "DELETE",
  });
}

/** Live connectivity probe — works for env and runtime connectors alike. */
export function testConnector(ref: string): Promise<ConnectorTestResponse> {
  return apiFetch<ConnectorTestResponse>(
    `/v1/connectors/${encodeURIComponent(ref)}/test`,
    { method: "POST" },
  );
}

// ---- Manifests ----

/** Registered executable units & agent runners — the resolvable manifest_ref values. */
export function listManifests(): Promise<ManifestSummary[]> {
  return apiFetch<ManifestSummary[]>("/v1/manifests");
}

// ---- Cost (deployment-scoped) ----

export async function getCost(): Promise<DeploymentCost> {
  const ref = await deploymentRef();
  return apiFetch<DeploymentCost>(`/v1/deployments/${encodeURIComponent(ref)}/cost`);
}

// ---- Studio ----

export function listWorkflows(): Promise<WorkflowSummary[]> {
  return apiFetch<WorkflowSummary[]>("/api/studio/v1/workflows");
}

export function getWorkflow(id: string): Promise<WorkflowDetail> {
  return apiFetch<WorkflowDetail>(`/api/studio/v1/workflows/${encodeURIComponent(id)}`);
}

export function createWorkflow(name: string): Promise<WorkflowDetail> {
  return apiFetch<WorkflowDetail>("/api/studio/v1/workflows", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function updateWorkflow(
  id: string,
  body: UpdateWorkflowRequest,
): Promise<WorkflowDetail> {
  return apiFetch<WorkflowDetail>(`/api/studio/v1/workflows/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function deleteWorkflow(id: string): Promise<void> {
  return apiFetch<void>(`/api/studio/v1/workflows/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/** Clone a published workflow into a fresh editable draft version (same id). */
export function cloneWorkflow(id: string): Promise<WorkflowDetail> {
  return apiFetch<WorkflowDetail>(
    `/api/studio/v1/workflows/${encodeURIComponent(id)}/clone`,
    { method: "POST" },
  );
}

export function listNodeTypes(): Promise<NodeType[]> {
  return apiFetch<NodeType[]>("/api/studio/v1/node-types");
}

export function errMsg(e: unknown): string {
  return e instanceof ApiError ? `${e.status || ""} ${e.message}`.trim() : String(e);
}
