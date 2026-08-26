// Information architecture for the console: the sidebar nav model + a
// route -> title map (used by the Topbar breadcrumb).

export type NavItem = { label: string; href: string; badge?: "approvals" };
export type NavGroup = { heading: string; items: NavItem[]; gated?: "regulus" };

export const NAV: NavGroup[] = [
  {
    heading: "Operate",
    items: [
      { label: "Overview", href: "/" },
      { label: "Runs", href: "/runs" },
      { label: "Approvals", href: "/approvals", badge: "approvals" },
      { label: "Audit", href: "/audit" },
      { label: "Deployments", href: "/deployments" },
    ],
  },
  {
    heading: "Build",
    items: [
      { label: "Studio", href: "/studio" },
      { label: "Templates", href: "/templates" },
      { label: "Connectors", href: "/connectors" },
      { label: "Repositories", href: "/repos" },
      { label: "Webhooks", href: "/webhooks" },
    ],
  },
  {
    heading: "Govern",
    items: [
      { label: "Cost", href: "/cost" },
      { label: "Retention", href: "/retention" },
      { label: "Rightsizing", href: "/rightsizing" },
      { label: "Metrics", href: "/metrics" },
    ],
  },
  {
    heading: "Regulus",
    gated: "regulus",
    items: [
      { label: "Econ Dashboard", href: "/regulus" },
      { label: "Capabilities", href: "/regulus/capabilities" },
      { label: "Enforcement", href: "/regulus/enforcement" },
      { label: "Costing", href: "/regulus/costing" },
      { label: "Reconciliation", href: "/regulus/reconciliation" },
    ],
  },
  { heading: "Learn", items: [{ label: "Guide", href: "/guide" }] },
];

export const TITLE: Record<string, string> = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.href, i.label])),
);
