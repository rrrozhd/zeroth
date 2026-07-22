"use client";

import { useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  fmtTime,
  Input,
  Mono,
  NotConnected,
  Skeleton,
  StatusBadge,
  PageHeader,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  createWebhookSubscription,
  deleteWebhookSubscription,
  errMsg,
  listWebhookDeadLetters,
  listWebhookSubscriptions,
  replayWebhookDeadLetter,
  type WebhookSubscription,
} from "@/app/lib/api";

export default function WebhooksPage() {
  const connected = useConnected();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Integrations"
        subtitle="Webhook subscriptions and failed-delivery replay."
      />
      {!connected ? (
        <NotConnected />
      ) : (
        <>
          <SubscriptionsCard />
          <DeadLettersCard />
        </>
      )}
    </div>
  );
}

function SubscriptionsCard() {
  const { data, error, loading, reload } = useAsync(listWebhookSubscriptions, []);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState("");
  const [busy, setBusy] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  // The secret is returned only once, on create — surface it prominently.
  const [created, setCreated] = useState<WebhookSubscription | null>(null);

  async function create() {
    const target = url.trim();
    const eventTypes = events.split(",").map((e) => e.trim()).filter(Boolean);
    if (!target || eventTypes.length === 0) {
      setCreateErr("Target URL and at least one event type are required.");
      return;
    }
    setBusy(true);
    setCreateErr(null);
    setCreated(null);
    try {
      const sub = await createWebhookSubscription(target, eventTypes);
      setCreated(sub);
      setUrl("");
      setEvents("");
      reload();
    } catch (e) {
      setCreateErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await deleteWebhookSubscription(id);
      reload();
    } catch {
      /* re-fetch reflects reality; keep the row quiet */
      reload();
    }
  }

  const subs = data?.subscriptions ?? [];

  return (
    <Card title="Webhook subscriptions">
      <div className="space-y-5">
        {error && <ApiErrorNote error={error} />}

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">
            Add a subscription
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Target URL" hint="where events are POSTed">
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/hooks/zeroth"
                className="font-mono"
              />
            </Field>
            <Field label="Event types" hint="comma-separated, e.g. run.completed, run.failed">
              <Input value={events} onChange={(e) => setEvents(e.target.value)} className="font-mono" />
            </Field>
          </div>
          {createErr && <ErrorBox message={createErr} />}
          {created && (
            <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900/60 dark:bg-emerald-950/40">
              <div className="font-medium text-emerald-800 dark:text-emerald-300">
                Subscription created — save the signing secret now, it won&apos;t be shown again:
              </div>
              <div className="mt-1 break-all font-mono text-xs text-emerald-900 dark:text-emerald-200">
                {created.secret}
              </div>
            </div>
          )}
          <Button variant="primary" onClick={create} disabled={busy}>
            {busy ? "Creating…" : "Create subscription"}
          </Button>
        </div>

        <div className="border-t border-border pt-4">
          {loading && !data && <Skeleton rows={2} />}
          {data && subs.length === 0 && (
            <Empty>No subscriptions yet — add one above.</Empty>
          )}
          {subs.length > 0 && (
            <ul className="divide-y divide-border">
              {subs.map((s) => (
                <li
                  key={s.subscription_id}
                  className="flex items-center justify-between gap-3 py-2.5 text-sm first:pt-0"
                >
                  <div className="min-w-0">
                    <div className="truncate font-mono text-xs">{s.target_url}</div>
                    <div className="mt-0.5 text-xs text-muted">
                      {s.event_types.join(", ")} · added {fmtTime(s.created_at)}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <StatusBadge status={s.active ? "active" : "cancelled"} />
                    <Button size="sm" variant="danger" onClick={() => remove(s.subscription_id)}>
                      Deactivate
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}

function DeadLettersCard() {
  const { data, error, loading, reload } = useAsync(listWebhookDeadLetters, []);
  const [replaying, setReplaying] = useState<string | null>(null);
  const items = data?.dead_letters ?? [];

  async function replay(id: string) {
    setReplaying(id);
    try {
      await replayWebhookDeadLetter(id);
      reload();
    } catch {
      reload();
    } finally {
      setReplaying(null);
    }
  }

  return (
    <Card
      title="Dead-lettered deliveries"
      actions={
        <Button onClick={() => reload()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      }
    >
      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={2} />}
      {data && items.length === 0 && (
        <Empty>No failed deliveries — everything has been delivered or retried successfully.</Empty>
      )}
      {items.length > 0 && (
        <ul className="divide-y divide-border">
          {items.map((d) => (
            <li key={d.dead_letter_id} className="flex items-center justify-between gap-3 py-2.5 text-sm first:pt-0">
              <div className="min-w-0">
                <div className="truncate">
                  <Mono>{d.event_type}</Mono>
                  <span className="ml-2 text-xs text-muted">{d.attempt_count} attempts</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-red-600 dark:text-red-400">
                  {d.last_status_code != null ? `HTTP ${d.last_status_code} · ` : ""}
                  {d.last_error ?? "delivery failed"} · {fmtTime(d.dead_lettered_at)}
                </div>
              </div>
              <Button
                size="sm"
                variant="primary"
                onClick={() => replay(d.dead_letter_id)}
                disabled={replaying === d.dead_letter_id}
              >
                {replaying === d.dead_letter_id ? "Replaying…" : "Replay"}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
