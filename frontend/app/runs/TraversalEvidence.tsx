"use client";

import { Card, MonoLabel, Pill } from "@/app/components/primitives";
import type { RunStatus } from "@/app/lib/api";

type Traversal = NonNullable<RunStatus["traversal"]>;
type RoutingDecision = NonNullable<Traversal["routing_decisions"]>[number];

function reasonLabel(reason: NonNullable<RoutingDecision["suppression_reason"]>): string {
  return reason.replaceAll("_", " ");
}

export function TraversalEvidence({ traversal }: { traversal?: Traversal }) {
  if (!traversal) return null;
  const decisions = traversal.routing_decisions ?? [];
  const visits = Object.entries(traversal.node_visit_counts ?? {}).sort((left, right) =>
    left[0].localeCompare(right[0]),
  );
  const hasVisitLimit = decisions.some(
    (decision) => decision.suppression_reason === "visit_limit",
  );

  if (visits.length === 0 && decisions.length === 0 && !traversal.stop_reason) {
    return null;
  }

  return (
    <Card label="Iterations & routing" pad={14}>
      <div style={{ display: "grid", gap: 14 }}>
        {visits.length > 0 && (
          <section aria-label="Node visit counts">
            <MonoLabel style={{ display: "block", marginBottom: 7 }}>Node visits</MonoLabel>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {visits.map(([nodeId, count]) => (
                <span
                  key={nodeId}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 7,
                    border: "1px solid var(--hair)",
                    borderRadius: 8,
                    padding: "6px 8px",
                    background: "var(--bg-card)",
                    fontSize: 11.5,
                  }}
                >
                  <span style={{ fontFamily: "var(--font-mono)" }}>{nodeId}</span>
                  <span style={{ color: "var(--text-muted)" }}>
                    {count} {count === 1 ? "visit" : "visits"}
                  </span>
                </span>
              ))}
            </div>
          </section>
        )}

        {decisions.length > 0 && (
          <section aria-label="Routing decisions">
            <MonoLabel style={{ display: "block", marginBottom: 7 }}>Routing decisions</MonoLabel>
            <div style={{ display: "grid", gap: 6 }}>
              {decisions.map((decision, index) => (
                <div
                  key={`${decision.condition_id}-${index}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 9,
                    minWidth: 0,
                    padding: "7px 0",
                    borderBottom: "1px solid var(--hair)",
                  }}
                >
                  <span style={{ minWidth: 0, flex: 1, fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                    {decision.condition_id}
                  </span>
                  <Pill tone={decision.suppression_reason ? "warning" : decision.matched ? "success" : "muted"}>
                    {decision.suppression_reason
                      ? reasonLabel(decision.suppression_reason)
                      : decision.matched
                        ? "matched"
                        : "not matched"}
                  </Pill>
                  {decision.selected_edge_id && (
                    <span style={{ fontFamily: "var(--font-mono)", fontSize: 10.5, color: "var(--text-muted)" }}>
                      {decision.selected_edge_id}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}

        {traversal.stop_reason === "branch_suppressed" && (
          <p
            role="status"
            style={{ margin: 0, fontSize: 11.5, lineHeight: 1.5, color: hasVisitLimit ? "var(--warning)" : "var(--text-muted)" }}
          >
            {hasVisitLimit
              ? "Safety limit stopped further traversal; inspect the loop condition before replaying."
              : "No route condition matched; the workflow stopped at this branch."}
          </p>
        )}
      </div>
    </Card>
  );
}
