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
export type StudioContract = S["StudioContractResponse"];
export type CreateDeploymentRequest = S["CreateDeploymentRequest"];
export type AuditVerification = S["AuditVerificationResponse"];

// The publish 422 payload — FastAPI types it as a generic validation error in
// the spec, but the studio API fills `detail` with this structured issue list
// (see publish_workflow in studio_api.py).
export type PublishIssue = {
  severity: string;
  code: string;
  message: string;
  node_id: string | null;
  edge_id: string | null;
};

// GET .../diff returns GraphDiff.model_dump() — typed as a plain object in the
// spec, so the shape is mirrored here (see zeroth.core.graph.diff).
export type DiffEntry = {
  entity_id: string;
  change_type: "added" | "removed" | "modified";
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  changed_fields: string[];
};

export type WorkflowDiff = {
  left_graph_id: string;
  left_version: number;
  right_graph_id: string;
  right_version: number;
  node_changes: DiffEntry[];
  edge_changes: DiffEntry[];
  condition_changes: DiffEntry[];
  contract_changes: DiffEntry[];
  policy_changes: DiffEntry[];
  memory_connector_changes: DiffEntry[];
  executable_unit_binding_changes: DiffEntry[];
};

export class ApiError extends Error {
  status: number;
  /** The raw `detail` from the error body — structured payloads (e.g. the
      publish issue list) survive here even though `message` is a string. */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.name = "ApiError";
  }
}

// Ceiling for any single request. A wedged backend (accepts connections,
// never replies — e.g. blocked middleware) must surface as an error, not an
// eternal loading state.
const REQUEST_TIMEOUT_MS = 20_000;

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = getApiBase();
  const key = getApiKey();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (key) headers.set("X-API-Key", key);

  let res: Response;
  try {
    res = await fetch(`${base}${path}`, {
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      ...init, // caller-supplied signal (if any) wins over the default
      headers,
    });
  } catch (e) {
    const where = base || "this origin";
    if (e instanceof DOMException && e.name === "TimeoutError") {
      throw new ApiError(
        0,
        `No response from ${where}${path} after ${REQUEST_TIMEOUT_MS / 1000}s — ` +
          "the service accepted the connection but never replied. It may be wedged; " +
          "try restarting it or check the API base under Connect.",
      );
    }
    throw new ApiError(0, `Network error reaching ${where}${path}: ${(e as Error).message}`);
  }

  if (!res.ok) {
    let message = res.statusText;
    let detail: unknown;
    try {
      const body = await res.json();
      detail = body?.detail;
      // Structured details (e.g. the publish issue list) stringify to noise —
      // surface their `message` and keep the raw payload on the error.
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail))
        message = detail
          .map((d) => (typeof d === "string" ? d : JSON.stringify(d)))
          .join("; ");
      else message = (detail as { message?: string })?.message ?? JSON.stringify(body);
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, message, detail);
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

/** Create a deployment version from a published graph (DEPLOYMENT_ADMIN).
    Serving it still requires a restart with ZEROTH_DEPLOYMENT_REF set. */
export function createDeployment(body: CreateDeploymentRequest): Promise<DeploymentSummary> {
  return apiFetch<DeploymentSummary>("/v1/deployments", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Verify the tamper-evident audit digest chain for one run. */
export function getRunAuditVerification(runId: string): Promise<AuditVerification> {
  return apiFetch<AuditVerification>(
    `/v1/runs/${encodeURIComponent(runId)}/audit-verification`,
  );
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

/** Validate and publish the draft. Validation failures reject with an
    ApiError whose `.detail.issues` is the structured PublishIssue list. */
export function publishWorkflow(id: string): Promise<WorkflowDetail> {
  return apiFetch<WorkflowDetail>(
    `/api/studio/v1/workflows/${encodeURIComponent(id)}/publish`,
    { method: "POST" },
  );
}

/** Pull the structured issue list out of a failed publish, if present. */
export function publishIssuesOf(e: unknown): PublishIssue[] | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  const issues = (e.detail as { issues?: unknown })?.issues;
  return Array.isArray(issues) ? (issues as PublishIssue[]) : null;
}

/** Registered contracts (latest version each) for contract-ref pickers. */
export function listContracts(): Promise<StudioContract[]> {
  return apiFetch<StudioContract[]>("/api/studio/v1/contracts");
}

/** Structured diff between two versions of a workflow. */
export function diffWorkflow(id: string, left: number, right: number): Promise<WorkflowDiff> {
  return apiFetch<WorkflowDiff>(
    `/api/studio/v1/workflows/${encodeURIComponent(id)}/diff?left=${left}&right=${right}`,
  );
}

export function listNodeTypes(): Promise<NodeType[]> {
  return apiFetch<NodeType[]>("/api/studio/v1/node-types");
}

export function errMsg(e: unknown): string {
  return e instanceof ApiError ? `${e.status || ""} ${e.message}`.trim() : String(e);
}
