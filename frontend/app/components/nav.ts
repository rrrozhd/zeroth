// Information architecture for the console: the sidebar nav model + a
// route -> title map (used by the Topbar breadcrumb).

export type NavItem = { label: string; href: string; badge?: "approvals" };
export type NavGroup = { heading: string; items: NavItem[]; gated?: "regulus" };

export const ECONOMICS_VIEWS = [
  { id: "spend", label: "Spend & budgets", href: "/cost", access: "metrics_read" },
  { id: "workflows", label: "Workflow economics", href: "/regulus", access: "metrics_read" },
  { id: "models", label: "Cost models", href: "/regulus/costing", access: "metrics_read" },
  { id: "reconciliation", label: "Reconciliation", href: "/regulus/reconciliation", access: "metrics_read" },
] as const;

export const NAV: NavGroup[] = [
  {
    heading: "Operate",
    items: [
      { label: "Overview", href: "/" },
      { label: "Runs", href: "/runs" },
      { label: "Approvals", href: "/approvals", badge: "approvals" },
      { label: "Audit", href: "/audit" },
      { label: "Deployments", href: "/deployments" },
      { label: "Artifacts", href: "/artifacts" },
    ],
  },
  {
    heading: "Build",
    items: [
      { label: "Studio", href: "/studio" },
      { label: "Templates", href: "/templates" },
      { label: "Connectors", href: "/connectors" },
      { label: "Webhooks", href: "/webhooks" },
    ],
  },
  {
    heading: "Govern",
    items: [
      { label: "Economics", href: "/cost" },
      { label: "Capabilities", href: "/regulus/capabilities" },
      { label: "Enforcement", href: "/regulus/enforcement" },
      { label: "Retention", href: "/retention" },
      { label: "Rightsizing", href: "/rightsizing" },
      { label: "Metrics", href: "/metrics" },
    ],
  },
  { heading: "Learn", items: [{ label: "Guide", href: "/guide" }] },
];

export const TITLE: Record<string, string> = {
  ...Object.fromEntries(NAV.flatMap((g) => g.items.map((i) => [i.href, i.label]))),
  ...Object.fromEntries(ECONOMICS_VIEWS.map((view) => [view.href, view.label])),
};
