// Client for the admin-gated Regulus proxy (`/v1/econ/regulus/*`).
//
// The main app exposes the bundled econ control plane to the console ONLY through
// that proxy (see backend regulus_proxy_api / REGULUS-FINDINGS.md); the console
// never calls `/regulus/*` directly. The proxy returns Regulus responses verbatim,
// so these shapes come 1:1 from `api-types.regulus.ts` (generated from the mounted
// /regulus OpenAPI). A console path suffix maps to the Regulus route minus its
// `/v1` prefix — e.g. Regulus `/v1/dashboard/kpis` → `/v1/econ/regulus/dashboard/kpis`.

import { apiFetch } from "./api";
import type { components as R } from "./api-types.regulus";

type RS = R["schemas"];
const P = "/v1/econ/regulus";

// Response types (verbatim from Regulus)
export type KPIResponse = RS["KPIResponse"];
export type CapabilityValueRow = RS["CapabilityValueRow"];
export type CapabilityRankingRow = RS["CapabilityRankingRow"];
export type TrendPoint = RS["TrendPoint"];
export type ConfidenceGateStatus = RS["ConfidenceGateStatus"];
export type DataQualityMix = RS["DataQualityMix"];
export type PolicyTimelineRow = RS["PolicyTimelineRow"];
export type ImplementationCompareRow = RS["ImplementationCompareRow"];
export type CapabilityOut = RS["CapabilityOut"];
export type ImplementationOut = RS["ImplementationOut"];
export type EnforcementActionOut = RS["EnforcementActionOut"];
export type PolicyActionOut = RS["PolicyActionOut"];
export type CostProfileOut = RS["CostProfileOut"];
export type EstimateOut = RS["ValueEstimateOut"];
export type CalibrationSummary = RS["CalibrationSummary"];

// --- Dashboard ---
export const rgKpis = () => apiFetch<KPIResponse>(`${P}/dashboard/kpis`);
export const rgTopCreators = () => apiFetch<CapabilityValueRow[]>(`${P}/dashboard/top-creators`);
export const rgCapitalDestroyers = () =>
  apiFetch<CapabilityValueRow[]>(`${P}/dashboard/capital-destroyers`);
export const rgCapabilityRanking = () =>
  apiFetch<CapabilityRankingRow[]>(`${P}/dashboard/capability-ranking`);
export const rgConfidenceTrend = () => apiFetch<TrendPoint[]>(`${P}/dashboard/confidence-trend`);
export const rgEfficiencyTrend = () => apiFetch<TrendPoint[]>(`${P}/dashboard/efficiency-trend`);
export const rgCalibrationTrend = () => apiFetch<TrendPoint[]>(`${P}/dashboard/calibration-trend`);
export const rgActionSuppression = () =>
  apiFetch<TrendPoint[]>(`${P}/dashboard/action-suppression`);
export const rgConfidenceGate = () =>
  apiFetch<ConfidenceGateStatus>(`${P}/dashboard/confidence-gate-status`);
export const rgDataQualityMix = () => apiFetch<DataQualityMix>(`${P}/dashboard/data-quality-mix`);
export const rgPolicyTimeline = () =>
  apiFetch<PolicyTimelineRow[]>(`${P}/dashboard/policy-timeline`);
export const rgDriftTimeline = (capabilityId: string) =>
  apiFetch<TrendPoint[]>(`${P}/dashboard/drift-timeline/${encodeURIComponent(capabilityId)}`);
export const rgImplementationCompare = (capabilityId: string) =>
  apiFetch<ImplementationCompareRow[]>(
    `${P}/dashboard/implementation-compare/${encodeURIComponent(capabilityId)}`,
  );

// --- Capabilities registry ---
export const rgCapabilities = () => apiFetch<CapabilityOut[]>(`${P}/registry/capabilities`);
export const rgCapability = (id: string) =>
  apiFetch<CapabilityOut>(`${P}/registry/capabilities/${encodeURIComponent(id)}`);
export const rgImplementation = (implementationId: string) =>
  apiFetch<ImplementationOut>(`${P}/registry/implementations/${encodeURIComponent(implementationId)}`);
export const rgEvaluationsLatest = (capabilityId: string) =>
  apiFetch<unknown>(`${P}/evaluations/${encodeURIComponent(capabilityId)}/latest`);
export const rgEvaluationsHistory = (capabilityId: string) =>
  apiFetch<unknown>(`${P}/evaluations/${encodeURIComponent(capabilityId)}/history`);

// --- Enforcement (read + approve/reject) ---
export const rgEnforcementActions = () =>
  apiFetch<EnforcementActionOut[]>(`${P}/enforcement/actions`);
export const rgPolicyActions = () => apiFetch<PolicyActionOut[]>(`${P}/enforcement/policy-actions`);
export const rgApproveAction = (actionId: number, reason?: string) =>
  apiFetch<EnforcementActionOut>(`${P}/enforcement/actions/${actionId}/approve`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? null }),
  });
export const rgRejectAction = (actionId: number, reason?: string) =>
  apiFetch<EnforcementActionOut>(`${P}/enforcement/actions/${actionId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? null }),
  });

// --- Costing / Performance ---
export const rgCostProfile = (profileId: string) =>
  apiFetch<CostProfileOut>(`${P}/costing/profiles/${encodeURIComponent(profileId)}`);
export const rgEstimateLatest = (capabilityId: string) =>
  apiFetch<EstimateOut>(`${P}/costing/estimates/${encodeURIComponent(capabilityId)}/latest`);
export const rgPerformanceSummary = () => apiFetch<unknown>(`${P}/performance/summary`);
export const rgPerformanceCapabilities = () => apiFetch<unknown>(`${P}/performance/capabilities`);

// --- Reconciliation ---
export const rgCalibrationSummary = () =>
  apiFetch<CalibrationSummary>(`${P}/reconciliation/calibration-summary`);
