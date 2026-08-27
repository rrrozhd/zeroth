import { Card, MonoLabel } from "@/app/components/primitives";

export function WebhookSummary() {
  return (
    <section>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          marginBottom: 10,
        }}
      >
        <MonoLabel>Webhooks</MonoLabel>
      </div>
      <Card pad={16}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="m-0 text-sm font-medium text-foreground">Delivery administration</p>
            <p className="mt-1 max-w-[68ch] text-xs leading-relaxed text-muted">
              Subscriptions, signed-delivery status, dead letters, replay, and deactivation live in
              one canonical workspace.
            </p>
          </div>
          <a
            href="/console/webhooks/"
            className="rounded-lg border border-border bg-raised px-3 py-2 text-sm font-medium text-foreground hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Manage webhooks
          </a>
        </div>
      </Card>
    </section>
  );
}
