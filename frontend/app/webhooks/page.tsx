"use client";

import { useState } from "react";
import { Button, Card, MonoLabel, Skeleton } from "@/app/components/primitives";
import { fmtTime, useAsync, useConnected } from "@/app/components/ui";
import {
  createWebhookSubscription,
  errMsg,
  getIdentity,
  listWebhookDeliveries,
  listWebhookDeadLetters,
  listWebhookSubscriptions,
  replayWebhookDeadLetter,
  type WebhookSubscriptionList,
  type WebhookSubscription,
} from "@/app/lib/api";
import { isForbiddenSurface } from "@/app/lib/surfaceAccess";
import {
  WEBHOOK_EVENT_TYPES,
  deactivateConfirmedWebhook,
  webhookFailureText,
} from "./webhook-ui";

export default function WebhooksPage() {
  const connected = useConnected();

  return (
    <div
      data-evidence-id="webhooks.page"
      style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}
    >
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ margin: 0, fontSize: 19, fontWeight: 600, letterSpacing: "-0.01em" }}>
          Webhooks
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 13, color: "var(--text-muted)" }}>
          Send selected run events to external systems and recover failed deliveries.
        </p>
      </header>

      {!connected ? (
        <StatePanel>
          Connect to the API from the top bar to manage webhook delivery.
        </StatePanel>
      ) : (
        <WebhooksAccessBoundary />
      )}
    </div>
  );
}

function WebhooksAccessBoundary() {
  const identity = useAsync(getIdentity, []);
  const subscriptionsRequest = useAsync(listWebhookSubscriptions, []);

  if (
    (identity.loading && !identity.data) ||
    (subscriptionsRequest.loading && !subscriptionsRequest.data && !subscriptionsRequest.error)
  ) {
    return <Skeleton height={74} />;
  }
  if (identity.error) {
    return (
      <div data-evidence-id="webhooks.access.error">
        <ErrorPanel message={`Identity unavailable: ${identity.error}`} compact />
      </div>
    );
  }
  if (!identity.data) return null;

  const workspace = identity.data.workspace_id ?? "tenant-wide";
  const roles = identity.data.roles.join(" · ") || "unavailable";
  const scope = `${identity.data.tenant_id} / ${workspace}`;

  if (subscriptionsRequest.error && isForbiddenSurface(subscriptionsRequest.error)) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <ScopePanel scope={scope} roles={roles} />
        <div data-evidence-id="webhooks.access.restricted">
          <StatePanel>
            <strong style={{ display: "block", color: "var(--text-primary)", marginBottom: 3 }}>
              Access restricted
            </strong>
            This API key cannot administer Webhooks. A built-in admin or platform admin role has
            access; configured roles may also receive the webhook:admin capability. This
            credential can remain connected for its permitted surfaces, but subscriptions,
            deliveries, and dead-letter actions are hidden.
          </StatePanel>
        </div>
      </div>
    );
  }
  if (subscriptionsRequest.error) {
    return (
      <div data-evidence-id="webhooks.access.error">
        <ErrorPanel
          message={`Webhook administration unavailable: ${subscriptionsRequest.error}`}
          compact
        />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
      <ScopePanel scope={scope} roles={roles} />
      <SubscriptionsSection subscriptionsRequest={subscriptionsRequest} />
      <DeliveriesSection />
      <DeadLettersSection />
    </div>
  );
}

function ScopePanel({ scope, roles }: { scope: string; roles: string }) {
  return (
    <div
      data-evidence-id="webhooks.scope"
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: 12,
        padding: "9px 12px",
        border: "1px solid var(--hair)",
        borderRadius: 8,
        color: "var(--text-muted)",
        fontSize: 11.5,
      }}
    >
      <span>Scope: {scope}</span>
      <span>Role: {roles}</span>
    </div>
  );
}

function DeliveriesSection() {
  const { data, error, loading, reload } = useAsync(listWebhookDeliveries, []);
  const deliveries = data?.deliveries ?? [];

  return (
    <section data-evidence-id="webhooks.deliveries.section">
      <SectionHeading
        title="Delivery activity"
        action={
          <Button
            onClick={() => reload()}
            disabled={loading}
            data-evidence-id="webhooks.deliveries.refresh"
          >
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />
      <Card pad={16}>
        {error && <ErrorPanel message={error} compact />}
        {loading && !data && <Skeleton height={42} />}
        {data && deliveries.length === 0 && (
          <StatePanel>No deliveries yet. Matching workflow events will appear here.</StatePanel>
        )}
        {deliveries.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column" }}>
            {deliveries.map((delivery, index) => (
              <div
                key={delivery.delivery_id}
                data-evidence-id={`webhooks.delivery.${delivery.delivery_id}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "9px 0",
                  borderTop: index === 0 ? "none" : "1px solid var(--hair)",
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 500 }}>{delivery.event_type}</div>
                  <CorrelationLine
                    runId={delivery.run_id}
                    approvalId={delivery.approval_id}
                    evidenceId={`webhooks.delivery.${delivery.delivery_id}.correlation`}
                  />
                  <div
                    style={{
                      marginTop: 3,
                      fontFamily: "var(--font-mono)",
                      fontSize: 10.5,
                      color: "var(--text-faint)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {delivery.delivery_id} · {delivery.attempt_count}/{delivery.max_attempts} attempts
                    {(delivery.last_status_code != null || delivery.last_error)
                      ? ` · ${webhookFailureText(delivery.last_status_code, delivery.last_error)}`
                      : ""} · {fmtTime(delivery.updated_at)}
                  </div>
                </div>
                <DeliveryStatus status={delivery.status} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </section>
  );
}

function SubscriptionsSection({
  subscriptionsRequest,
}: {
  subscriptionsRequest: {
    data: WebhookSubscriptionList | null;
    error: string | null;
    loading: boolean;
    reload: (background?: boolean) => Promise<void>;
  };
}) {
  const { data, error, loading, reload } = subscriptionsRequest;
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [created, setCreated] = useState<WebhookSubscription | null>(null);
  const [removeErr, setRemoveErr] = useState<string | null>(null);

  async function create() {
    const target = url.trim();
    if (!target || events.length === 0) {
      setCreateErr("Target URL and at least one event type are required.");
      return;
    }
    setBusy(true);
    setCreateErr(null);
    setCreated(null);
    try {
      const subscription = await createWebhookSubscription(target, events);
      setCreated(subscription);
      setUrl("");
      setEvents([]);
      reload();
    } catch (cause) {
      setCreateErr(errMsg(cause));
    } finally {
      setBusy(false);
    }
  }

  function toggleEvent(eventType: string) {
    setEvents((current) =>
      current.includes(eventType)
        ? current.filter((candidate) => candidate !== eventType)
        : [...current, eventType],
    );
  }

  async function remove(subscription: WebhookSubscription) {
    setRemoveErr(null);
    try {
      const removed = await deactivateConfirmedWebhook(
        subscription.subscription_id,
        subscription.target_url,
      );
      if (removed) reload();
    } catch (cause) {
      setRemoveErr(errMsg(cause));
    }
  }

  const subscriptions = data?.subscriptions ?? [];

  return (
    <section data-evidence-id="webhooks.subscriptions.section">
      <SectionHeading title="Subscriptions" />
      <Card pad={16}>
        <MonoLabel style={{ display: "block", marginBottom: 12 }}>Add a subscription</MonoLabel>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
            gap: 12,
            alignItems: "start",
          }}
        >
          <Field label="Target URL" hint="HTTPS endpoint receiving POST requests">
            <TextInput
              value={url}
              onChange={setUrl}
              placeholder="https://example.com/hooks/zeroth"
              evidenceId="webhooks.target-url"
            />
          </Field>
          <Field label="Event types" hint="Choose every event this endpoint should receive">
            <div
              data-evidence-id="webhooks.events"
              style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "7px 12px" }}
            >
              {WEBHOOK_EVENT_TYPES.map((eventType) => (
                <label
                  key={eventType}
                  style={{ display: "inline-flex", alignItems: "center", gap: 7, minHeight: 26 }}
                >
                  <input
                    type="checkbox"
                    checked={events.includes(eventType)}
                    onChange={() => toggleEvent(eventType)}
                    data-evidence-id={`webhooks.event.${eventType}`}
                  />
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>{eventType}</span>
                </label>
              ))}
            </div>
          </Field>
        </div>

        <div style={{ marginTop: 12 }}>
          <Button
            variant="primary"
            onClick={create}
            disabled={busy}
            data-evidence-id="webhooks.create"
          >
            {busy ? "Creating…" : "Create subscription"}
          </Button>
        </div>

        {createErr && <ErrorPanel message={createErr} />}
        {removeErr && <ErrorPanel message={`Deactivation failed: ${removeErr}`} />}
        {created && <SecretPanel subscription={created} />}

        <div style={{ borderTop: "1px solid var(--hair)", marginTop: 16, paddingTop: 14 }}>
          <MonoLabel style={{ display: "block", marginBottom: 8 }}>Active records</MonoLabel>
          {error && <ErrorPanel message={error} compact />}
          {loading && !data && <Skeleton height={42} />}
          {data && subscriptions.length === 0 && (
            <StatePanel>No subscriptions configured.</StatePanel>
          )}
          {subscriptions.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column" }}>
              {subscriptions.map((subscription, index) => (
                <div
                  key={subscription.subscription_id}
                  data-evidence-id={`webhooks.subscription.${subscription.subscription_id}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    minHeight: 44,
                    padding: "8px 0",
                    borderTop: index === 0 ? "none" : "1px solid var(--hair)",
                  }}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div
                      title={subscription.target_url}
                      style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: 11.5,
                        color: "var(--text-primary)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {subscription.target_url}
                    </div>
                    <div style={{ marginTop: 3, fontSize: 11, color: "var(--text-faint)" }}>
                      {subscription.event_types.join(", ")} · added {fmtTime(subscription.created_at)}
                    </div>
                  </div>
                  <Status active={subscription.active} />
                  <Button
                    variant="danger"
                    onClick={() => remove(subscription)}
                    style={{ padding: "5px 9px" }}
                    data-evidence-id={`webhooks.subscription.${subscription.subscription_id}.deactivate`}
                  >
                    Deactivate
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>
    </section>
  );
}

function DeadLettersSection() {
  const { data, error, loading, reload } = useAsync(listWebhookDeadLetters, []);
  const [replaying, setReplaying] = useState<string | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const deliveries = data?.dead_letters ?? [];

  async function replay(id: string) {
    setReplaying(id);
    setReplayError(null);
    try {
      await replayWebhookDeadLetter(id);
      reload();
    } catch (cause) {
      setReplayError(
        `Replay failed: ${cause instanceof Error ? cause.message : errMsg(cause)}`,
      );
    } finally {
      setReplaying(null);
    }
  }

  return (
    <section data-evidence-id="webhooks.dead-letters.section">
      <SectionHeading
        title="Dead-lettered deliveries"
        action={
          <Button
            onClick={() => reload()}
            disabled={loading}
            data-evidence-id="webhooks.dead-letters.refresh"
          >
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />
      <Card pad={16}>
        {error && <ErrorPanel message={error} compact />}
        {replayError && <ErrorPanel message={replayError} compact />}
        {loading && !data && <Skeleton height={42} />}
        {data && deliveries.length === 0 && (
          <StatePanel>No failed deliveries. The queue is clear.</StatePanel>
        )}
        {deliveries.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column" }}>
            {deliveries.map((delivery, index) => (
              <div
                key={delivery.dead_letter_id}
                data-evidence-id={`webhooks.dead-letter.${delivery.dead_letter_id}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "9px 0",
                  borderTop: index === 0 ? "none" : "1px solid var(--hair)",
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 500 }}>{delivery.event_type}</div>
                  <CorrelationLine
                    runId={delivery.run_id}
                    approvalId={delivery.approval_id}
                    evidenceId={`webhooks.dead-letter.${delivery.dead_letter_id}.correlation`}
                  />
                  <div
                    style={{
                      marginTop: 3,
                      fontFamily: "var(--font-mono)",
                      fontSize: 10.5,
                      color: "var(--danger)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {webhookFailureText(delivery.last_status_code, delivery.last_error)} ·{" "}
                    {delivery.attempt_count} attempts ·{" "}
                    {fmtTime(delivery.dead_lettered_at)}
                  </div>
                </div>
                <Button
                  variant="primary"
                  onClick={() => replay(delivery.dead_letter_id)}
                  disabled={replaying === delivery.dead_letter_id}
                  style={{ padding: "5px 10px" }}
                  data-evidence-id={`webhooks.dead-letter.${delivery.dead_letter_id}.replay`}
                >
                  {replaying === delivery.dead_letter_id ? "Replaying…" : "Replay"}
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </section>
  );
}

function CorrelationLine({
  runId,
  approvalId,
  evidenceId,
}: {
  runId?: string | null;
  approvalId?: string | null;
  evidenceId: string;
}) {
  if (!runId && !approvalId) return null;
  return (
    <div
      data-evidence-id={evidenceId}
      style={{
        marginTop: 3,
        color: "var(--text-muted)",
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}
    >
      {[runId ? `run ${runId}` : null, approvalId ? `approval ${approvalId}` : null]
        .filter(Boolean)
        .join(" · ")}
    </div>
  );
}

function SectionHeading({
  title,
  action,
}: {
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        marginBottom: 8,
      }}
    >
      <h2 style={{ margin: 0, fontSize: 13, fontWeight: 600 }}>{title}</h2>
      {action}
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "block" }}>
      <span style={{ display: "block", fontSize: 12, fontWeight: 500 }}>{label}</span>
      <span style={{ display: "block", margin: "2px 0 6px", fontSize: 10.5, color: "var(--text-faint)" }}>
        {hint}
      </span>
      {children}
    </label>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  evidenceId,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  evidenceId: string;
}) {
  return (
    <input
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      data-evidence-id={evidenceId}
      autoComplete="off"
      style={{
        width: "100%",
        boxSizing: "border-box",
        padding: "8px 10px",
        border: "1px solid var(--hair-strong)",
        borderRadius: 8,
        background: "var(--bg-card)",
        color: "var(--text-primary)",
        fontFamily: "var(--font-mono)",
        fontSize: 11.5,
        outline: "none",
      }}
    />
  );
}

function StatePanel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        padding: "13px 14px",
        borderRadius: 8,
        background: "var(--bg-raised)",
        color: "var(--text-muted)",
        fontSize: 12,
        lineHeight: 1.45,
      }}
    >
      {children}
    </div>
  );
}

function ErrorPanel({ message, compact = false }: { message: string; compact?: boolean }) {
  return (
    <div
      style={{
        marginTop: compact ? 0 : 12,
        padding: compact ? "9px 11px" : "11px 12px",
        borderRadius: 8,
        border: "1px solid color-mix(in srgb, var(--danger) 30%, transparent)",
        background: "color-mix(in srgb, var(--danger) 6%, var(--bg-card))",
        color: "var(--danger)",
        fontSize: 11.5,
      }}
    >
      {message}
    </div>
  );
}

function SecretPanel({ subscription }: { subscription: WebhookSubscription }) {
  const [revealed, setRevealed] = useState(false);

  return (
    <div
      data-evidence-id="webhooks.secret.panel"
      style={{
        marginTop: 12,
        padding: "11px 12px",
        borderRadius: 8,
        border: "1px solid color-mix(in srgb, var(--success) 28%, transparent)",
        background: "color-mix(in srgb, var(--success) 6%, var(--bg-card))",
      }}
    >
      <div style={{ fontSize: 11.5, color: "var(--success)" }}>
        Subscription created. The signing secret is hidden by default and will not be shown again
        after leaving this page.
      </div>
      <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
        <Button
          onClick={() => setRevealed((current) => !current)}
          data-evidence-id="webhooks.secret.reveal"
          aria-pressed={revealed}
          style={{ padding: "5px 9px" }}
        >
          {revealed ? "Hide secret" : "Reveal secret"}
        </Button>
        <code
          data-evidence-id="webhooks.secret.value"
          aria-label={revealed ? "Signing secret" : "Signing secret hidden"}
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            color: "var(--text-primary)",
            overflowWrap: "anywhere",
          }}
        >
          {revealed ? subscription.secret : "••••••••••••••••••••"}
        </code>
      </div>
    </div>
  );
}

function Status({ active }: { active: boolean }) {
  const color = active ? "var(--success)" : "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        flexShrink: 0,
        fontSize: 11,
        color,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: color }} />
      {active ? "active" : "inactive"}
    </span>
  );
}

function DeliveryStatus({ status }: { status: string }) {
  const color = status === "delivered"
    ? "var(--success)"
    : status === "failed" || status === "dead_letter"
      ? "var(--danger)"
      : "var(--text-muted)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        flexShrink: 0,
        fontSize: 11,
        color,
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: color }} />
      {status.replace("_", " ")}
    </span>
  );
}
