// Typed API client for the Zeroth Console.
//
// Types come from app/lib/api-types.ts, generated from the platform's
// /openapi.json via `npm run gen:api` (openapi-typescript), so request/response
// shapes cannot drift from the backend. The thin apiFetch wrapper adds the
// runtime API base + X-API-Key from lib/config.ts.

import type { components, operations } from "./api-types";
import { getApiBase, getApiKey } from "./config";

type S = components["schemas"];

export type HealthResponse = S["HealthResponse"];
export type IdentityResponse = S["IdentityResponse"];
export type RunStatus = S["RunStatusResponse"];
export type RunInvocationResponse = S["RunInvocationResponse"];
export type AdminRunList = S["AdminRunListResponse"];
export type ChildRunSummary = S["ChildRunSummaryResponse"];
export type ApprovalRecord = S["ApprovalRecord"];
export type ApprovalResolutionRequest = S["ApprovalResolutionRequest"];
export type ApprovalResolutionResponse = S["ApprovalResolutionResponse"];
export type OperationResolutionRequest = S["OperationResolutionRequest"];
export type OperationResolutionResponse = S["OperationResolutionResponse"];
export type AuditRecordList = S["AuditRecordListResponse"];
export type TenantAuditRecordList = S["TenantAuditRecordListResponse"];
export type AuditTimeline = S["AuditTimelineResponse"];
export type NodeAuditRecord = S["NodeAuditRecord"];
export type DeploymentCost = S["DeploymentCostResponse"];
export type DeploymentSummary = S["DeploymentSummaryResponse"];
export type ConnectorSummary = S["ConnectorSummaryResponse"];
export type ConnectorCreateRequest = S["ConnectorCreateRequest"];
export type ConnectorUpdateRequest = S["ConnectorUpdateRequest"];
export type ConnectorTestResponse = S["ConnectorTestResponse"];
export type ManifestSummary = S["ManifestSummaryResponse"];
export type ManifestDetail = S["ManifestDetailResponse"];
export type ManifestRunList = S["ManifestRunListResponse"];
export type WorkflowSummary = S["WorkflowSummaryResponse"];
export type WorkflowDetail = S["WorkflowDetailResponse"];
export type WorkflowPreflight = S["WorkflowPreflightResponse"];
export type LiveProviderVerification = S["LiveProviderVerificationResponse"];
export type UpdateWorkflowRequest = S["UpdateWorkflowRequest"];
export type NodeType = S["NodeTypeResponse"];
export type StudioNode = S["StudioNodeResponse"];
export type StudioEdge = S["StudioEdgeResponse"];
export type StudioEdgeInput = S["StudioEdgeInput"];
export type StudioViewport = S["StudioViewport"];
type StudioContract = S["StudioContractResponse"];
export type CreateContractRequest = S["CreateContractRequest"];
export type CreateDeploymentRequest = S["CreateDeploymentRequest"];
export type AuditVerification = S["AuditVerificationResponse"];
export type AuditReadiness = S["AuditReadinessResponse"];
export type DeploymentAttestation = S["DeploymentAttestationResponse"];
export type AttestationVerification = S["AttestationVerificationResponse"];
export type RollbackDeploymentRequest = S["RollbackDeploymentRequest"];
export type RetrievedArtifact = {
  artifactId: string;
  bytes: Uint8Array;
  mediaType: string;
  size: number;
};
export type CertificationResponse = S["CertificationResponse"];
export type RegisterCertificationRequest = S["RegisterCertificationRequest"];
export type PromoteCertificationRequest = S["PromoteCertificationRequest"];
export type RevokeCertificationRequest = S["RevokeCertificationRequest"];
export type CertificationOverrideRequest = S["CertificationOverrideRequest"];
export type GuardrailPolicyPatch = S["GuardrailPolicyPatch"];
export type GuardrailPolicyResponse = S["GuardrailPolicyResponse"];

// GET /v1/metrics has no fixed schema — the OpenAPI spec types its 200 body as
// an open object, so the generated operation's response type (`unknown`) flows
// through unchanged rather than being hand-narrowed to a fabricated shape.
export type MetricsResponse =
  operations["get_metrics_v1_metrics_get"]["responses"][200]["content"]["application/json"];

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

// Model right-sizing (POST /v1/econ/rightsizing) — hand-declared like PublishIssue
// above: the shape mirrors zeroth.core.econ.rightsizing and is stable, so it isn't
// worth threading through the generated spec.
export type RightsizingOption = {
  model: string;
  provider: string;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  blended_per_mtok_usd: number;
  savings_pct: number;
  max_input_tokens: number | null;
  supports_tools: boolean;
  supports_vision: boolean;
  same_provider: boolean;
};

export type RightsizingResult = {
  incumbent: string;
  incumbent_known: boolean;
  incumbent_provider: string | null;
  incumbent_blended_per_mtok_usd: number | null;
  needs_tools: boolean;
  needs_vision: boolean;
  assumption: string;
  candidates: RightsizingOption[];
  note: string;
};

// Measured right-sizing (POST /v1/econ/rightsizing/experiment) — mirrors
// zeroth.core.econ.rightsizing_experiment.
export type CandidateOutcome = {
  model: string;
  provider: string;
  is_incumbent: boolean;
  equivalence_rate: number;
  error_rate: number;
  cases_evaluated: number;
  cases_errored: number;
  est_cost_per_1k_calls_usd: number | null;
  savings_pct: number | null;
  capability_ok: boolean;
  meets_bar: boolean;
};

export type HarvestStats = {
  cases: number;
  skipped_not_success: number;
  skipped_used_tools: number;
  skipped_empty_output: number;
  skipped_other_model: number;
  mean_input_tokens: number;
  mean_output_tokens: number;
  token_profile_measured: boolean;
};

export type ExperimentCallEvidence = {
  operation_id: string;
  provider_request_id: string | null;
  cost_event_id: string | null;
  audit_event_id: string | null;
  model: string;
  cost_measurement: string;
  measured_cost_usd: number | null;
  estimated_cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cleanup_status: string;
  provider_call_attempted: boolean;
  cache_hit: boolean;
};

export type ExperimentExecutionEvidence = {
  run_id: string;
  campaign_id: string | null;
  provider_call_count: number;
  measured_cost_usd: number;
  estimated_cost_usd: number;
  calls: ExperimentCallEvidence[];
};

export type ExperimentReport = {
  incumbent: string;
  node_id: string | null;
  mode: "equivalence" | "correctness";
  cases: number;
  min_cases: number;
  tolerance_pct: number;
  incumbent_self_equivalence: number;
  mean_input_tokens: number;
  mean_output_tokens: number;
  token_profile_measured: boolean;
  harvest: HarvestStats | null;
  outcomes: CandidateOutcome[];
  recommended_model: string | null;
  verdict: "confirmed" | "flagged" | "none";
  note: string;
  execution?: ExperimentExecutionEvidence | null;
};

// Passive right-sizing opportunities (GET /v1/econ/rightsizing/opportunities) — mirrors
// zeroth.core.econ.opportunities.
export type NodeSpend = {
  node_id: string;
  source_deployment_ref?: string | null;
  runs: number;
  total_cost_usd: number;
  mean_cost_per_call_usd: number;
  total_estimated_cost_usd: number;
  mean_estimated_cost_per_call_usd: number;
  incumbent_model: string | null;
  uses_tools: boolean;
  tool_free_runs: number;
  cheaper_alternatives: number;
  best_savings_pct: number | null;
  projected_savings_usd: number | null;
  projected_estimated_savings_usd: number | null;
  experiment_ready: boolean;
};

export type SpendReport = {
  total_cost_usd: number;
  total_estimated_cost_usd: number;
  nodes: NodeSpend[];
  note: string;
};

// Unit economics (GET /v1/econ/unit-economics) — mirrors zeroth.core.econ.unit_economics.
export type WorkflowEconomics = {
  workflow_name: string;
  runs: number;
  successful_runs: number;
  failed_runs: number;
  success_rate: number;
  terminal_cost_usd: number;
  cost_per_successful_run_usd: number | null;
  failure_tax_usd: number;
  estimated_terminal_cost_usd: number;
  estimated_cost_per_successful_run_usd: number | null;
  estimated_failure_tax_usd: number;
};

export type TenantEconomics = {
  tenant_id: string;
  runs: number;
  successful_runs: number;
  failed_runs: number;
  success_rate: number;
  terminal_cost_usd: number;
  cost_per_successful_run_usd: number | null;
  failure_tax_usd: number;
  estimated_terminal_cost_usd: number;
  estimated_cost_per_successful_run_usd: number | null;
  estimated_failure_tax_usd: number;
};

type QualityEconomics = {
  terminal_runs: number;
  labeled_terminal_runs: number;
  coverage: number;
  quality_successes: number;
  quality_success_rate_over_labeled: number;
  cost_per_quality_success_usd: number | null;
  cost_on_quality_failures_usd: number;
  sources: string[];
  state: "ok" | "not_configured" | "below_coverage_floor";
  note: string;
};

export type UnitEconomicsReport = {
  window_runs: number;
  successful_runs: number;
  failed_runs: number;
  in_flight_runs: number;
  success_rate: number;
  total_cost_usd: number;
  terminal_cost_usd: number;
  cost_on_successful_usd: number;
  cost_on_failed_usd: number;
  cost_on_in_flight_usd: number;
  cost_per_successful_run_usd: number | null;
  mean_cost_per_successful_run_usd: number | null;
  failure_tax_usd: number;
  failure_tax_ratio: number;
  estimated_total_cost_usd: number;
  estimated_terminal_cost_usd: number;
  estimated_cost_on_successful_usd: number;
  estimated_cost_on_failed_usd: number;
  estimated_cost_on_in_flight_usd: number;
  estimated_cost_per_successful_run_usd: number | null;
  estimated_mean_cost_per_successful_run_usd: number | null;
  estimated_failure_tax_usd: number;
  estimated_failure_tax_ratio: number;
  runs_with_cost: number;
  runs_with_estimated_cost: number;
  by_workflow: WorkflowEconomics[];
  by_tenant: TenantEconomics[];
  quality: QualityEconomics | null;
  note: string;
};

// Economic waste rollup (GET /v1/econ/waste) — mirrors zeroth.core.econ.waste.
export type WasteRollupFinding = {
  kind: string;
  node_id: string | null;
  wasted_usd: number;
  confirmed: boolean;
  severity: string;
  detail: string;
  metadata: Record<string, unknown>;
  run_id: string;
};

export type WasteKindTotal = { kind: string; count: number; wasted_usd: number };

export type WasteRollup = {
  window_runs: number;
  runs_with_waste: number;
  runs_with_cost: number;
  total_cost_usd: number;
  total_confirmed_waste_usd: number;
  total_flagged_waste_usd: number;
  waste_ratio: number;
  findings: number;
  confirmed_findings: number;
  flagged_findings: number;
  by_kind: WasteKindTotal[];
  top_findings: WasteRollupFinding[];
  note: string;
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
const GET_TRANSPORT_RETRY_BACKOFF_MS = 100;

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = getApiBase();
  const key = getApiKey();
  // Every console data route is protected. Reject before `fetch` so eager
  // client effects cannot create misleading "unauthenticated" audit events
  // while the user is still configuring the connection.
  if (!key) {
    throw new ApiError(0, "Connect to the API before loading protected data.");
  }
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (key) headers.set("X-API-Key", key);

  // Reuse one signal across both attempts so a recovery attempt cannot double
  // the request's overall deadline. Caller-owned cancellation remains
  // authoritative and is never converted into a retry.
  const signal = init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  const method = (init.method ?? "GET").toUpperCase();
  const fetchRequest = () =>
    fetch(`${base}${path}`, {
      ...init,
      signal,
      headers,
    });

  let res: Response | null = null;
  let transportError: unknown;
  try {
    res = await fetchRequest();
  } catch (e) {
    transportError = e;
    if (method === "GET" && !signal.aborted) {
      // Yield before retrying so WebKit/Chromium do not immediately reuse the
      // same stale cross-origin connection after a local backend replacement.
      // The shared signal keeps this delay inside the original request budget.
      await new Promise((resolve) => setTimeout(resolve, GET_TRANSPORT_RETRY_BACKOFF_MS));
      try {
        if (!signal.aborted) res = await fetchRequest();
      } catch (retryError) {
        transportError = retryError;
      }
    }
    if (res === null) {
      const where = base || "this origin";
      if (transportError instanceof DOMException && transportError.name === "TimeoutError") {
        throw new ApiError(
          0,
          `No response from ${where}${path} after ${REQUEST_TIMEOUT_MS / 1000}s — ` +
            "the service accepted the connection but never replied. It may be wedged; " +
            "try restarting it or check the API base under Connect.",
        );
      }
      throw new ApiError(
        0,
        `Network error reaching ${where}${path}: ${(transportError as Error).message}`,
      );
    }
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

/** Authenticated identity, role, tenant and workspace carried by the active key. */
export function getIdentity(): Promise<IdentityResponse> {
  return apiFetch<IdentityResponse>("/v1/identity");
}

/** The deployment ref currently served by the connected API.
 *
 * Do not cache this value: the persistent development service may restart on
 * the same origin while serving a newly deployed graph. A stale ref turns all
 * deployment-scoped screens into misleading 404s until the browser process is
 * discarded.
 */
export async function deploymentRef(): Promise<string> {
  const health = await getHealth();
  return health.deployment_ref;
}

// ---- Artifacts ----

export async function getArtifact(artifactId: string): Promise<RetrievedArtifact> {
  const key = getApiKey();
  if (!key) {
    throw new ApiError(0, "Connect to the API before loading protected data.");
  }
  const response = await fetch(
    `${getApiBase()}/v1/artifacts/${encodeURIComponent(artifactId)}`,
    {
      headers: { Accept: "*/*", "X-API-Key": key },
      signal: AbortSignal.timeout(20_000),
    },
  );
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      // Artifact errors may be non-JSON; the transport status remains useful.
    }
    throw new ApiError(response.status, message);
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  return {
    artifactId,
    bytes,
    mediaType: response.headers.get("content-type") ?? "application/octet-stream",
    size: bytes.byteLength,
  };
}

// ---- Runs ----

export function listRuns(): Promise<AdminRunList> {
  return apiFetch<AdminRunList>("/v1/admin/runs");
}

export function getRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/v1/runs/${encodeURIComponent(runId)}`);
}

export function getChildRuns(runId: string): Promise<ChildRunSummary[]> {
  return apiFetch<ChildRunSummary[]>(`/v1/runs/${encodeURIComponent(runId)}/children`);
}

export function submitRun(body: {
  input_payload?: Record<string, unknown>;
  thread_id?: string | null;
  campaign_id?: string | null;
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

export async function listAudits(): Promise<TenantAuditRecordList> {
  return apiFetch<TenantAuditRecordList>("/v1/admin/audits");
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

/** Roll a deployment back to an earlier graph version: creates a new deployment
    version pinned to `targetGraphVersion` (DEPLOYMENT_ADMIN). Like createDeployment,
    actually serving the new version still requires a restart. The endpoint
    requires the target graph version in the body — the Overview derives it from a
    deployment row's `graph_version_ref` (`{graph_id}@{version}`). */
export function rollbackDeployment(
  ref: string,
  targetGraphVersion: number,
): Promise<DeploymentSummary> {
  const body: RollbackDeploymentRequest = { target_graph_version: targetGraphVersion };
  return apiFetch<DeploymentSummary>(
    `/v1/deployments/${encodeURIComponent(ref)}/rollback`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---- Production certification ----

export function listCertifications(): Promise<CertificationResponse[]> {
  return apiFetch<CertificationResponse[]>("/v1/certifications");
}

export function getCertification(id: string): Promise<CertificationResponse> {
  return apiFetch<CertificationResponse>(`/v1/certifications/${encodeURIComponent(id)}`);
}

export function registerCertification(
  body: RegisterCertificationRequest,
): Promise<CertificationResponse> {
  return apiFetch<CertificationResponse>("/v1/certifications", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function promoteCertification(id: string): Promise<CertificationResponse> {
  return apiFetch<CertificationResponse>(
    `/v1/certifications/${encodeURIComponent(id)}/promote`,
    { method: "POST", body: JSON.stringify({} satisfies PromoteCertificationRequest) },
  );
}

export function revokeCertification(
  id: string,
  body: RevokeCertificationRequest,
): Promise<CertificationResponse> {
  return apiFetch<CertificationResponse>(
    `/v1/certifications/${encodeURIComponent(id)}/revoke`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function overrideCertification(
  id: string,
  body: CertificationOverrideRequest,
): Promise<CertificationResponse> {
  return apiFetch<CertificationResponse>(
    `/v1/certifications/${encodeURIComponent(id)}/override`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// ---- Metrics ----

/** Runtime metrics snapshot for the connected service. The spec leaves the
    response body open (no fixed schema), so the type is `unknown` — callers that
    consume specific keys must narrow at the use site. */
export function getMetrics(): Promise<MetricsResponse> {
  return apiFetch<MetricsResponse>("/v1/metrics");
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

/** Safe manifest detail; source, commands, environment and secret bindings stay server-side. */
export function getManifest(manifestRef: string): Promise<ManifestDetail> {
  return apiFetch<ManifestDetail>(`/v1/manifests/${encodeURIComponent(manifestRef)}`);
}

/** Audit-authorized run/node identities linked to a registered executable unit. */
export function listManifestRuns(manifestRef: string): Promise<ManifestRunList> {
  return apiFetch<ManifestRunList>(
    `/v1/manifests/${encodeURIComponent(manifestRef)}/runs`,
  );
}

// ---- Model right-sizing (authoring-time nudge) ----

/** Cheaper, capability-compatible alternatives to a node's model. Candidates to
    A/B test — capability + price only, never a quality verdict. */
export function getRightsizing(body: {
  incumbent: string;
  needs_tools?: boolean;
  needs_vision?: boolean;
  min_savings_pct?: number;
  limit?: number;
}): Promise<RightsizingResult> {
  return apiFetch<RightsizingResult>("/v1/econ/rightsizing", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Passive right-sizing opportunities: agent nodes ranked by spend × savings potential.
    Read-only aggregation over the audit trail — no LLM calls. */
export function getRightsizingOpportunities(): Promise<SpendReport> {
  return apiFetch<SpendReport>("/v1/econ/rightsizing/opportunities");
}

/** Unit economics: cost per *successful* outcome and the failure tax over the last N runs.
    Read-only aggregation over the run + audit trail — no LLM calls. */
export function getUnitEconomics(): Promise<UnitEconomicsReport> {
  return apiFetch<UnitEconomicsReport>("/v1/econ/unit-economics?scope=tenant");
}

export type EconomicsConfiguration = S["EconomicsConfigurationResponse"];

export function getEconomicsConfiguration(): Promise<EconomicsConfiguration> {
  return apiFetch<EconomicsConfiguration>("/v1/econ/configuration");
}

/** Deployment-wide structural-waste rollup (paid-for-failed, loops, retries) over the last N runs. */
export function getWaste(): Promise<WasteRollup> {
  return apiFetch<WasteRollup>("/v1/econ/waste?scope=tenant");
}

/** Measured right-sizing: replays the node's real inputs through cheaper models and
    scores equivalence to the incumbent. Real LLM calls — override the default 20s
    timeout with a generous ceiling so a small experiment can finish. */
export function runRightsizingExperiment(body: {
  node_id: string;
  source_deployment_ref?: string;
  incumbent: string;
  instruction: string;
  needs_tools?: boolean;
  needs_vision?: boolean;
  judge_model?: string;
  max_candidates?: number;
  max_cases?: number;
  min_cases?: number;
  tolerance_pct?: number;
  mode?: "equivalence" | "correctness";
}): Promise<ExperimentReport> {
  return apiFetch<ExperimentReport>("/v1/econ/rightsizing/experiment", {
    method: "POST",
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(180_000),
  });
}

/** Restore the latest completed measured experiment in the active deployment scope. */
export function getLatestRightsizingExperiment(): Promise<ExperimentReport | null> {
  return apiFetch<ExperimentReport | null>("/v1/econ/rightsizing/experiment/latest");
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

/** Validate graph structure and resolve runtime dependencies without executing nodes. */
export function preflightWorkflow(id: string): Promise<WorkflowPreflight> {
  return apiFetch<WorkflowPreflight>(
    `/api/studio/v1/workflows/${encodeURIComponent(id)}/preflight`,
    { method: "POST" },
  );
}

/** Make bounded paid provider probes after explicit UI acknowledgement. */
export function verifyWorkflowProviders(id: string): Promise<LiveProviderVerification> {
  return apiFetch<LiveProviderVerification>(
    `/api/studio/v1/workflows/${encodeURIComponent(id)}/verify-provider`,
    {
      method: "POST",
      body: JSON.stringify({
        acknowledge_external_call: true,
        timeout_seconds: 15,
        max_models: 3,
      }),
    },
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

/** Register a schema-only contract authored in the console (WORKFLOW_ADMIN).
    Re-posting an existing name creates the next version. */
export function createContract(body: CreateContractRequest): Promise<StudioContract> {
  return apiFetch<StudioContract>("/api/studio/v1/contracts", {
    method: "POST",
    body: JSON.stringify(body),
  });
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

// ---------------------------------------------------------------------------
// P1 Operate — admin run actions, run evidence/chain, deployment detail.
// ---------------------------------------------------------------------------

export type RunEvidence = S["RunEvidenceResponse"];
export type DeploymentMetadata = S["DeploymentVersionMetadataResponse"];
export type PublicContractSchema = S["PublicContractSchemaResponse"];
export type DeploymentResultErrorStateSchema = S["DeploymentResultErrorStateSchemaResponse"];
export type DeploymentEvidence = S["DeploymentEvidenceResponse"];

export function cancelRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/v1/admin/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}

export function interruptRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/v1/admin/runs/${encodeURIComponent(runId)}/interrupt`, {
    method: "POST",
  });
}

export function replayRun(runId: string): Promise<RunStatus> {
  return apiFetch<RunStatus>(`/v1/admin/runs/${encodeURIComponent(runId)}/replay`, {
    method: "POST",
  });
}

export function getRunEvidence(runId: string): Promise<RunEvidence> {
  return apiFetch<RunEvidence>(`/v1/runs/${encodeURIComponent(runId)}/evidence`);
}

export function verifyRunChain(runId: string): Promise<AuditVerification> {
  return apiFetch<AuditVerification>(`/v1/runs/${encodeURIComponent(runId)}/verify-chain`, {
    method: "POST",
  });
}

/** Record an authorized determination for a durable AMBIGUOUS side effect.
    This resolves only the operation record; it does not resume or replay a run. */
export function resolveAmbiguousOperation(
  deploymentRef: string,
  operationKey: string,
  body: OperationResolutionRequest,
): Promise<OperationResolutionResponse> {
  return apiFetch<OperationResolutionResponse>(
    `/v1/deployments/${encodeURIComponent(deploymentRef)}/operations/${encodeURIComponent(operationKey)}/resolve`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// Deployment detail — ref-parameterized (the older attestation/cost/audit
// helpers default to `deploymentRef()`; the Deployments screen targets a
// selected ref explicitly).
export function getDeploymentMetadata(ref: string): Promise<DeploymentMetadata> {
  return apiFetch<DeploymentMetadata>(`/v1/deployments/${encodeURIComponent(ref)}/metadata`);
}

export function getDeploymentGuardrails(ref: string): Promise<GuardrailPolicyResponse> {
  return apiFetch<GuardrailPolicyResponse>(
    `/v1/deployments/${encodeURIComponent(ref)}/guardrails`,
  );
}

export function updateDeploymentGuardrails(
  ref: string,
  body: GuardrailPolicyPatch,
): Promise<GuardrailPolicyResponse> {
  return apiFetch<GuardrailPolicyResponse>(
    `/v1/deployments/${encodeURIComponent(ref)}/guardrails`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export function getDeploymentTimeline(ref: string): Promise<AuditTimeline> {
  return apiFetch<AuditTimeline>(`/v1/deployments/${encodeURIComponent(ref)}/timeline`);
}

export function getDeploymentEvidence(ref: string): Promise<DeploymentEvidence> {
  return apiFetch<DeploymentEvidence>(`/v1/deployments/${encodeURIComponent(ref)}/evidence`);
}

export function getInputContract(ref: string): Promise<PublicContractSchema> {
  return apiFetch<PublicContractSchema>(
    `/v1/deployments/${encodeURIComponent(ref)}/input-contract`,
  );
}

export function getOutputContract(ref: string): Promise<PublicContractSchema> {
  return apiFetch<PublicContractSchema>(
    `/v1/deployments/${encodeURIComponent(ref)}/output-contract`,
  );
}

export function getResultErrorStateSchema(
  ref: string,
): Promise<DeploymentResultErrorStateSchema> {
  return apiFetch<DeploymentResultErrorStateSchema>(
    `/v1/deployments/${encodeURIComponent(ref)}/result-error-state-schema`,
  );
}

export function verifyDeploymentAuditChain(ref: string): Promise<AuditVerification> {
  return apiFetch<AuditVerification>(
    `/v1/deployments/${encodeURIComponent(ref)}/audit-verification`,
  );
}

export function getAuditReadiness(): Promise<AuditReadiness> {
  return apiFetch<AuditReadiness>("/v1/audit-readiness");
}

// Attestation + cost for an explicitly selected deployment reference.

/** The persisted attestation for a specific deployment `ref` (WS-D). */
export function getAttestationOf(ref: string): Promise<DeploymentAttestation> {
  return apiFetch<DeploymentAttestation>(
    `/v1/deployments/${encodeURIComponent(ref)}/attestation`,
  );
}

/** GET self-verify: the server recomputes the digest + checks the signature of
    its own persisted attestation for `ref`. */
export function getAttestationVerifyOf(ref: string): Promise<AttestationVerification> {
  return apiFetch<AttestationVerification>(
    `/v1/deployments/${encodeURIComponent(ref)}/attestation/verify`,
  );
}

/** POST verify: submit an attestation body back to the server for verification.
    The endpoint requires the attestation payload, so the persisted attestation is
    fetched first and then round-tripped — proving the on-screen copy re-verifies. */
export async function postVerifyAttestationOf(
  ref: string,
): Promise<AttestationVerification> {
  const attestation = await getAttestationOf(ref);
  return apiFetch<AttestationVerification>(
    `/v1/deployments/${encodeURIComponent(ref)}/verify-attestation`,
    { method: "POST", body: JSON.stringify(attestation) },
  );
}

/** Cumulative spend for a specific deployment `ref`. */
export function getCostOf(ref: string): Promise<DeploymentCost> {
  return apiFetch<DeploymentCost>(`/v1/deployments/${encodeURIComponent(ref)}/cost`);
}

// ---------------------------------------------------------------------------
// P2 Build — templates + webhooks. List endpoints return envelopes.
// ---------------------------------------------------------------------------

export type TemplateList = S["TemplateListResponse"];
export type Template = S["TemplateResponse"];
export type CreateTemplateRequest = S["CreateTemplateRequest"];

export function listTemplates(): Promise<TemplateList> {
  return apiFetch<TemplateList>("/v1/templates");
}
export function getTemplate(name: string): Promise<Template> {
  return apiFetch<Template>(`/v1/templates/${encodeURIComponent(name)}`);
}
export function createTemplate(body: CreateTemplateRequest): Promise<Template> {
  return apiFetch<Template>("/v1/templates", { method: "POST", body: JSON.stringify(body) });
}
export function deleteTemplateVersion(name: string, version: string): Promise<void> {
  return apiFetch<void>(
    `/v1/templates/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
    { method: "DELETE" },
  );
}

export type WebhookSubscription = S["WebhookSubscriptionResponse"];
export type WebhookSubscriptionList = S["WebhookSubscriptionListResponse"];
export type CreateSubscriptionRequest = S["CreateSubscriptionRequest"];
export type DeadLetter = S["WebhookDeadLetterResponse"];
export type WebhookDeadLetterList = S["WebhookDeadLetterListResponse"];
export type WebhookDelivery = S["WebhookDeliveryResponse"];
export type WebhookDeliveryList = S["WebhookDeliveryListResponse"];

export function listWebhookSubscriptions(): Promise<WebhookSubscriptionList> {
  return apiFetch<WebhookSubscriptionList>("/v1/webhooks/subscriptions");
}
export function createWebhookSubscription(
  body: CreateSubscriptionRequest,
): Promise<WebhookSubscription>;
export function createWebhookSubscription(
  targetUrl: string,
  eventTypes: string[],
): Promise<WebhookSubscription>;
export async function createWebhookSubscription(
  bodyOrTarget: CreateSubscriptionRequest | string,
  eventTypes?: string[],
): Promise<WebhookSubscription> {
  const body =
    typeof bodyOrTarget === "string"
      ? {
          deployment_ref: await deploymentRef(),
          event_types: eventTypes ?? [],
          target_url: bodyOrTarget,
          tenant_id: "default",
        }
      : bodyOrTarget;
  return apiFetch<WebhookSubscription>("/v1/webhooks/subscriptions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
export function deleteWebhookSubscription(id: string): Promise<void> {
  return apiFetch<void>(`/v1/webhooks/subscriptions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
export function listWebhookDeliveries(): Promise<WebhookDeliveryList> {
  return apiFetch<WebhookDeliveryList>("/v1/webhooks/deliveries");
}
export function listDeadLetters(): Promise<WebhookDeadLetterList> {
  return apiFetch<WebhookDeadLetterList>("/v1/webhooks/dead-letters");
}
export function replayDeadLetter(id: string): Promise<void> {
  return apiFetch<void>(`/v1/webhooks/dead-letters/${encodeURIComponent(id)}/replay`, {
    method: "POST",
  });
}

// Destination-only Integrations page compatibility aliases. Keep one transport
// implementation while the rebuilt Connectors page uses the shorter names.
export const listWebhookDeadLetters = listDeadLetters;
export const replayWebhookDeadLetter = replayDeadLetter;

// ---------------------------------------------------------------------------
// P3 Govern — tenant cost/budget, retention, econ quality verdict.
// ---------------------------------------------------------------------------

export type TenantCost = S["TenantCostResponse"];
export type TenantBudgetRequest = S["TenantBudgetRequest"];

export function getTenantCost(tenantId: string): Promise<TenantCost> {
  return apiFetch<TenantCost>(`/v1/tenants/${encodeURIComponent(tenantId)}/cost`);
}
export function setTenantBudget(
  tenantId: string,
  body: TenantBudgetRequest,
): Promise<TenantCost> {
  return apiFetch<TenantCost>(`/v1/tenants/${encodeURIComponent(tenantId)}/budget`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export type RetentionPolicy = S["RetentionPolicyResponse"];
export type RetentionPolicyBody = S["RetentionPolicyBody"];
export type LegalHold = S["LegalHoldResponse"];
export type LegalHoldBody = S["LegalHoldBody"];
export type ErasureResult = S["ErasureResponse"];
export type ErasureRequestBody = S["ErasureRequestBody"];
export type ErasureHistoryEntry = S["ErasureHistoryEntry"];

export function getRetentionPolicy(): Promise<RetentionPolicy> {
  return apiFetch<RetentionPolicy>("/v1/retention/policy");
}
export function putRetentionPolicy(body: RetentionPolicyBody): Promise<RetentionPolicy> {
  return apiFetch<RetentionPolicy>("/v1/retention/policy", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
export function placeLegalHold(body: LegalHoldBody): Promise<LegalHold> {
  return apiFetch<LegalHold>("/v1/retention/legal-holds", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
export function listLegalHolds(): Promise<LegalHold[]> {
  return apiFetch<LegalHold[]>("/v1/retention/legal-holds");
}
export function releaseLegalHold(holdId: string): Promise<LegalHold> {
  return apiFetch<LegalHold>(`/v1/retention/legal-holds/${encodeURIComponent(holdId)}`, {
    method: "DELETE",
  });
}
export function requestErasure(body: ErasureRequestBody): Promise<ErasureResult> {
  return apiFetch<ErasureResult>("/v1/retention/erasure-requests", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
export function listErasureHistory(limit = 50): Promise<ErasureHistoryEntry[]> {
  return apiFetch<ErasureHistoryEntry[]>(`/v1/retention/erasure-history?limit=${limit}`);
}

export type QualityVerdictRequest = S["QualityVerdictRequest"];
export type RunQualityVerdict = S["RunQualityVerdict"];

export function attachQualityVerdict(body: QualityVerdictRequest): Promise<RunQualityVerdict> {
  return apiFetch<RunQualityVerdict>("/v1/econ/quality-verdict", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
