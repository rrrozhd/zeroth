export type RouteCase = {
  id: string;
  href: string;
  file: string;
  asyncStates?: boolean;
};

export const ROUTE_CASES: RouteCase[] = [
  { id: "nav-operate-overview", href: "/", file: "app/page.tsx", asyncStates: true },
  { id: "nav-operate-runs", href: "/runs", file: "app/runs/page.tsx", asyncStates: true },
  { id: "nav-operate-approvals", href: "/approvals", file: "app/approvals/page.tsx", asyncStates: true },
  { id: "nav-operate-audit", href: "/audit", file: "app/audit/page.tsx", asyncStates: true },
  { id: "nav-operate-deployments", href: "/deployments", file: "app/deployments/page.tsx", asyncStates: true },
  { id: "nav-operate-artifacts", href: "/artifacts", file: "app/artifacts/page.tsx", asyncStates: true },
  { id: "nav-build-studio", href: "/studio", file: "app/studio/page.tsx", asyncStates: true },
  { id: "nav-build-studio-edit", href: "/studio/edit", file: "app/studio/edit/page.tsx", asyncStates: true },
  { id: "nav-build-templates", href: "/templates", file: "app/templates/page.tsx", asyncStates: true },
  { id: "nav-build-connectors", href: "/connectors", file: "app/connectors/page.tsx", asyncStates: true },
  { id: "nav-build-repos", href: "/repos", file: "app/repos/page.tsx", asyncStates: true },
  { id: "nav-build-webhooks", href: "/webhooks", file: "app/webhooks/page.tsx", asyncStates: true },
  { id: "nav-govern-cost", href: "/cost", file: "app/cost/page.tsx", asyncStates: true },
  { id: "nav-govern-retention", href: "/retention", file: "app/retention/page.tsx", asyncStates: true },
  { id: "nav-govern-rightsizing", href: "/rightsizing", file: "app/rightsizing/page.tsx", asyncStates: true },
  { id: "nav-govern-metrics", href: "/metrics", file: "app/metrics/page.tsx", asyncStates: true },
  { id: "nav-regulus-dashboard", href: "/regulus", file: "app/regulus/page.tsx", asyncStates: true },
  { id: "nav-regulus-capabilities", href: "/regulus/capabilities", file: "app/regulus/capabilities/page.tsx", asyncStates: true },
  { id: "nav-regulus-enforcement", href: "/regulus/enforcement", file: "app/regulus/enforcement/page.tsx", asyncStates: true },
  { id: "nav-regulus-costing", href: "/regulus/costing", file: "app/regulus/costing/page.tsx", asyncStates: true },
  { id: "nav-regulus-reconciliation", href: "/regulus/reconciliation", file: "app/regulus/reconciliation/page.tsx", asyncStates: true },
  { id: "nav-learn-guide", href: "/guide", file: "app/guide/page.tsx" },
];

export const STATE_CASE_IDS = [
  "state-loading",
  "state-empty",
  "state-error",
  "state-connect",
  "state-regulus-unavailable",
  "state-regulus-404-empty",
  "state-metrics-text",
  "state-audit-signature",
] as const;

export const POLLING_MUTATION_CASES: Record<string, { file: string; markers: string[] }> = {
  "poll-runs": { file: "app/runs/page.tsx", markers: ["usePolling"] },
  "poll-approval-badge": { file: "app/components/AppShell.tsx", markers: ["usePolling", "approval"] },
  "poll-regulus-probe": { file: "app/components/AppShell.tsx", markers: ["detectRegulus"] },
  "mutation-approval": { file: "app/approvals/page.tsx", markers: ["optimistic", "toast"] },
  "mutation-deployment-create": { file: "app/deployments/page.tsx", markers: ["createDeployment", "toast"] },
  "mutation-deployment-rollback": { file: "app/deployments/page.tsx", markers: ["rollbackDeployment", "toast"] },
  "mutation-audit-verify": { file: "app/audit/page.tsx", markers: ["verify", "toast"] },
  "mutation-connector": { file: "app/connectors/page.tsx", markers: ["testConnector", "toast"] },
  "mutation-webhook": { file: "app/connectors/page.tsx", markers: ["replay", "toast"] },
  "mutation-budget": { file: "app/cost/page.tsx", markers: ["setTenantBudget", "toast"] },
  "mutation-retention": { file: "app/retention/page.tsx", markers: ["erasure", "toast"] },
  "mutation-rightsizing": { file: "app/rightsizing/page.tsx", markers: ["experiment", "error"] },
  "mutation-regulus-enforcement": { file: "app/regulus/enforcement/page.tsx", markers: ["approve", "reject"] },
};

export const STUDIO_CASES: Record<string, string[]> = {
  "studio-placement": ["beginPlacement", "onPaneClick={placeNode}", "placementType"],
  "studio-edge": ["onConnect", "sourceHandle === \"tools\"", "edgeKindProps"],
  "studio-config": ["NodeInspector"],
  "studio-publish": ["publish", "PublishIssuesContext"],
  "studio-clone": ["clone"],
  "studio-deploy": ["canDeployWorkflow"],
  "studio-run": ["canRunWorkflow"],
  "studio-readonly": ["read-only"],
};
