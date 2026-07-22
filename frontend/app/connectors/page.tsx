"use client";

// The Connectors screen — a flat operator/authoring view over two surfaces this
// deployment exposes: memory connectors (the resolvable connector_ref values) and
// webhook delivery (subscriptions + the dead-letter queue).
//
// Rebuilt fresh on the P0 primitives — it does NOT import the legacy ui.tsx or
// ConnectorInline. The backend-catalogue helper (lib/connectorBackends) is reused
// for the create/edit form's backend list + param shaping.
//
// FIELD MAPPING (renders only real wire fields — see api-types.ts):
//   ConnectorSummaryResponse: ref (name), connector_type (type), backend, scope
//     (the MemoryScope the connector is attached to → the "attached-to" column),
//     source ("env" | "runtime"), backend_type, params (secret-masked | null).
//     env-sourced connectors are read-only here (the API 409s on edit/delete), so
//     Edit/Delete only render for runtime connectors. Test works for both.
//   WebhookSubscriptionResponse: event_types[], target_url, active, subscription_id.
//     `secret` is deliberately never rendered.
//   WebhookDeadLetterResponse: subscription_id, dead_letter_id, event_type — grouped
//     by subscription_id to get each subscription's dead-letter count N.
//
// Every mutation (create/update/delete connector, test, add/delete subscription,
// replay DLQ) fires a toast and refetches. The API key lives only in localStorage
// (lib/config) — never logged, never in a URL. Nothing here crashes when the API is
// unconfigured or unreachable: useLoad turns failures into an inline error state.

import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  MonoLabel,
  Skeleton,
  StatusDot,
  TONE,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  createConnector,
  createWebhookSubscription,
  deleteConnector,
  deleteWebhookSubscription,
  errMsg,
  listConnectors,
  listDeadLetters,
  listWebhookSubscriptions,
  replayDeadLetter,
  testConnector,
  updateConnector,
  type ConnectorSummary,
  type DeadLetter,
  type WebhookDeadLetterList,
  type WebhookSubscription,
  type WebhookSubscriptionList,
} from "@/app/lib/api";
import {
  BACKENDS,
  backendSpec,
  buildParams,
  REF_RE,
  requiredParamsOk,
  seedParamValues,
  type BackendSpec,
  type ParamField,
} from "@/app/lib/connectorBackends";
import { getTenant, isConfigured } from "@/app/lib/config";

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function ConnectorsPage() {
  // localStorage-derived config is read after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  return (
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Connectors</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Memory &amp; retrieval backends this deployment can resolve, and webhook delivery.
        </p>
      </header>

      <MemoryConnectors connected={connected} mounted={mounted} />
      <div style={{ height: 24 }} />
      <Webhooks connected={connected} mounted={mounted} />
    </div>
  );
}

// --------------------------------------------------------------------------
// Memory connectors — table + inline create/edit form
// --------------------------------------------------------------------------

// name | type | backend | attached-to (scope) | status | actions.
// minmax(0,…) on the flexible tracks so long refs/params ellipsize instead of
// forcing horizontal page scroll (handoff §6 caveat).
const COLS = "minmax(0,1.5fr) minmax(0,0.85fr) minmax(0,1.1fr) minmax(0,0.85fr) minmax(0,1.15fr) minmax(184px,auto)";

function MemoryConnectors({ connected, mounted }: { connected: boolean; mounted: boolean }) {
  const connectors = useLoad<ConnectorSummary[]>(listConnectors);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ConnectorSummary | null>(null);

  const rows = connectors.data ?? [];

  function openCreate() {
    setEditing(null);
    setFormOpen(true);
  }
  function openEdit(c: ConnectorSummary) {
    setEditing(c);
    setFormOpen(true);
  }
  function closeForm() {
    setFormOpen(false);
    setEditing(null);
  }
  function onSaved() {
    closeForm();
    connectors.reload();
  }

  return (
    <section>
      <SectionHead label="Memory connectors">
        {connected && (
          <Button variant="primary" onClick={openCreate} style={{ padding: "4px 9px" }}>
            + New connector
          </Button>
        )}
      </SectionHead>

      <p style={{ margin: "0 0 12px", fontSize: 12, color: "var(--text-faint)", lineHeight: 1.55 }}>
        <b style={{ color: "var(--text-muted)" }}>env</b> connectors come from deployment settings
        and are read-only here; <b style={{ color: "var(--text-muted)" }}>runtime</b> connectors are
        stored in the platform DB and editable. Test works for both.
      </p>

      {connectors.loading && !connectors.data ? (
        <Card pad={14}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} height={30} />
            ))}
          </div>
        </Card>
      ) : connectors.error ? (
        <InlineError message={connectors.error} onRetry={connectors.reload} />
      ) : mounted && !connected ? (
        <Card pad={16}>
          <EmptyInline>Connect to the API (top bar) to load connectors.</EmptyInline>
        </Card>
      ) : rows.length === 0 ? (
        <Card pad={16}>
          <EmptyInline>No connectors registered — add one with + New connector.</EmptyInline>
        </Card>
      ) : (
        <Card pad={0} style={{ overflow: "hidden" }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: COLS,
              gap: 12,
              padding: "10px 16px",
              borderBottom: "1px solid var(--hair)",
            }}
          >
            <ColHead>name</ColHead>
            <ColHead>type</ColHead>
            <ColHead>backend</ColHead>
            <ColHead>attached-to</ColHead>
            <ColHead>status</ColHead>
            <span />
          </div>
          {rows.map((c) => (
            <ConnectorRow key={c.ref} connector={c} onEdit={openEdit} onChanged={connectors.reload} />
          ))}
        </Card>
      )}

      {formOpen && (
        <div style={{ marginTop: 16 }}>
          <ConnectorForm
            key={editing?.ref ?? "__new__"}
            editing={editing}
            onSaved={onSaved}
            onCancel={closeForm}
          />
        </div>
      )}
    </section>
  );
}

type TestState =
  | { phase: "idle" }
  | { phase: "testing" }
  | { phase: "ok"; latency: number; detail: string | null }
  | { phase: "fail"; latency: number; detail: string | null }
  | { phase: "error"; msg: string };

function ConnectorRow({
  connector: c,
  onEdit,
  onChanged,
}: {
  connector: ConnectorSummary;
  onEdit: (c: ConnectorSummary) => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [test, setTest] = useState<TestState>({ phase: "idle" });
  const [deleting, setDeleting] = useState(false);
  const isRuntime = c.source === "runtime";

  async function doTest() {
    setTest({ phase: "testing" });
    try {
      const r = await testConnector(c.ref);
      const ms = Math.round(r.latency_ms);
      const detail = r.detail ?? null;
      const tail = detail ? ` · ${detail}` : "";
      if (r.ok) {
        setTest({ phase: "ok", latency: ms, detail });
        toast(`✓ ${c.ref} ok · ${ms}ms${tail}`);
      } else {
        setTest({ phase: "fail", latency: ms, detail });
        toast(`✗ ${c.ref} failed · ${ms}ms${tail}`);
      }
    } catch (e) {
      const msg = errMsg(e);
      setTest({ phase: "error", msg });
      toast(`✗ ${c.ref} test error: ${msg}`);
    }
  }

  async function doDelete() {
    if (
      typeof window !== "undefined" &&
      !window.confirm(`Delete connector "${c.ref}"? Nodes referencing it will stop resolving.`)
    ) {
      return;
    }
    setDeleting(true);
    try {
      await deleteConnector(c.ref);
      toast(`Deleted connector ${c.ref}`);
      onChanged();
    } catch (e) {
      toast(`Delete failed: ${errMsg(e)}`);
      setDeleting(false);
    }
  }

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: COLS,
        gap: 12,
        alignItems: "center",
        padding: "11px 16px",
        borderBottom: "1px solid var(--hair)",
      }}
    >
      {/* name + source */}
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
        <span
          title={c.ref}
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 12.5,
            fontWeight: 600,
            color: "var(--text-primary)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {c.ref}
        </span>
        <MiniTag tone={isRuntime ? "accent" : "muted"}>{c.source}</MiniTag>
      </div>

      {/* type */}
      <CellText>{c.connector_type}</CellText>

      {/* backend (+ backend_type when runtime-configured) */}
      <div style={{ minWidth: 0 }}>
        <CellText>{c.backend}</CellText>
        {c.backend_type && (
          <span
            style={{
              display: "block",
              fontFamily: "var(--font-mono)",
              fontSize: 10.5,
              color: "var(--text-faint)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {c.backend_type}
          </span>
        )}
      </div>

      {/* attached-to (memory scope) */}
      <CellText mono>{c.scope}</CellText>

      {/* status + test result */}
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
        <StatusChip tone="success">registered</StatusChip>
        {test.phase !== "idle" && <TestResult test={test} />}
      </div>

      {/* actions */}
      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", flexShrink: 0 }}>
        <Button onClick={doTest} disabled={test.phase === "testing"} style={{ padding: "5px 9px" }}>
          {test.phase === "testing" ? "Testing…" : "Test"}
        </Button>
        {isRuntime && (
          <>
            <Button onClick={() => onEdit(c)} style={{ padding: "5px 9px" }}>
              Edit
            </Button>
            <Button
              variant="danger"
              onClick={doDelete}
              disabled={deleting}
              style={{ padding: "5px 9px" }}
            >
              {deleting ? "…" : "Delete"}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function TestResult({ test }: { test: TestState }) {
  if (test.phase === "ok")
    return (
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 10.5, color: "var(--success)" }}>
        ok · {test.latency}ms
      </span>
    );
  if (test.phase === "fail")
    return (
      <span
        title={test.detail ?? undefined}
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 10.5,
          color: "var(--danger)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        failed · {test.latency}ms
      </span>
    );
  if (test.phase === "error")
    return (
      <span
        title={test.msg}
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 10.5,
          color: "var(--danger)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        error
      </span>
    );
  return null;
}

// --------------------------------------------------------------------------
// Connector create/edit form — ConnectorCreateRequest is { ref, backend_type,
// params }; ConnectorUpdateRequest is { backend_type, params } (ref is fixed).
// Params are shaped from the backend catalogue (lib/connectorBackends). Secrets
// come back masked from the API and are never round-tripped — on edit, required
// secrets must be re-entered.
// --------------------------------------------------------------------------

function ConnectorForm({
  editing,
  onSaved,
  onCancel,
}: {
  editing: ConnectorSummary | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const initialBackend = editing?.backend_type ?? BACKENDS[0].value;
  const [backendType, setBackendType] = useState(initialBackend);
  const [ref, setRef] = useState(editing?.ref ?? "");
  const [values, setValues] = useState<Record<string, string>>(() =>
    editing ? seedParamValues(backendSpec(initialBackend), editing.params ?? {}) : {},
  );
  const [busy, setBusy] = useState(false);

  const spec = backendSpec(backendType);
  const refOk = editing ? true : REF_RE.test(ref.trim());
  const requiredOk = requiredParamsOk(spec, values);
  const canSubmit = requiredOk && (editing ? true : ref.trim().length > 0 && refOk) && !busy;

  function onBackendChange(next: string) {
    setBackendType(next);
    setValues({}); // param fields differ per backend — reset
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    try {
      const params = buildParams(spec, values);
      if (editing) {
        await updateConnector(editing.ref, { backend_type: backendType, params });
        toast(`Updated connector ${editing.ref}`);
      } else {
        const created = await createConnector({ ref: ref.trim(), backend_type: backendType, params });
        toast(`Created connector ${created.ref}`);
      }
      onSaved();
    } catch (err) {
      toast(`${editing ? "Update" : "Create"} failed: ${errMsg(err)}`);
      setBusy(false);
    }
  }

  return (
    <Card pad={16}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
        <span style={{ fontSize: 15, fontWeight: 600 }}>
          {editing ? `Edit connector: ${editing.ref}` : "New connector"}
        </span>
      </div>
      <p style={{ margin: "0 0 14px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.55 }}>
        {spec.desc}
      </p>

      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="backend" hint="The runtime backend that stores this connector's memory.">
          <SelectInput value={backendType} onChange={onBackendChange}>
            {BACKENDS.map((b) => (
              <option key={b.value} value={b.value}>
                {b.label}
              </option>
            ))}
          </SelectInput>
        </Field>

        {!editing && (
          <Field label="ref" hint="Lowercase letters, digits, _ and - only.">
            <TextInput value={ref} onChange={setRef} placeholder="docs_store" autoFocus />
            {ref.trim() !== "" && !refOk && (
              <span style={{ display: "block", marginTop: 5, fontSize: 11, color: "var(--danger)" }}>
                Ref must be lowercase letters, digits, _ or -.
              </span>
            )}
          </Field>
        )}

        <ParamFields spec={spec} values={values} onChange={setValues} reenterSecrets={!!editing} />

        <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
          <Button type="submit" variant="primary" disabled={!canSubmit}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create connector"}
          </Button>
          <Button type="button" variant="neutral" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

function ParamFields({
  spec,
  values,
  onChange,
  reenterSecrets,
}: {
  spec: BackendSpec;
  values: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  reenterSecrets: boolean;
}) {
  function set(key: string, v: string) {
    onChange({ ...values, [key]: v });
  }
  return (
    <>
      {spec.fields.map((f: ParamField) => {
        const secret = f.kind === "secret";
        const hint =
          secret && reenterSecrets
            ? `${f.hint ?? ""} Re-enter to change (never shown).`.trim()
            : f.hint;
        return (
          <Field
            key={f.key}
            label={f.label + (f.required ? " *" : "")}
            hint={hint}
          >
            <TextInput
              value={values[f.key] ?? ""}
              onChange={(v) => set(f.key, v)}
              placeholder={f.placeholder}
              type={secret ? "password" : "text"}
              inputMode={f.kind === "number" ? "numeric" : undefined}
            />
          </Field>
        );
      })}
    </>
  );
}

// --------------------------------------------------------------------------
// Webhooks — subscriptions + dead-letter status. Two loads: the subscription
// list and the DLQ (grouped by subscription_id for per-row counts).
// --------------------------------------------------------------------------

function Webhooks({ connected, mounted }: { connected: boolean; mounted: boolean }) {
  const subs = useLoad<WebhookSubscriptionList>(listWebhookSubscriptions);
  const dlq = useLoad<WebhookDeadLetterList>(listDeadLetters);
  const [formOpen, setFormOpen] = useState(false);

  const subList = subs.data?.subscriptions ?? [];
  const deadLetters = dlq.data?.dead_letters ?? [];

  // subscription_id -> its dead-letter entries.
  const dlqBySub = useMemo(() => {
    const map = new Map<string, DeadLetter[]>();
    for (const d of deadLetters) {
      const arr = map.get(d.subscription_id) ?? [];
      arr.push(d);
      map.set(d.subscription_id, arr);
    }
    return map;
  }, [deadLetters]);

  function reloadAll() {
    subs.reload();
    dlq.reload();
  }
  function onAdded() {
    setFormOpen(false);
    reloadAll();
  }

  return (
    <section>
      <SectionHead label="Webhooks">
        {connected && (
          <Button variant="primary" onClick={() => setFormOpen((v) => !v)} style={{ padding: "4px 9px" }}>
            + New subscription
          </Button>
        )}
      </SectionHead>

      <Card pad={0} style={{ overflow: "hidden" }}>
        {subs.loading && !subs.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 16 }}>
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} height={28} />
            ))}
          </div>
        ) : subs.error ? (
          <div style={{ padding: 16 }}>
            <InlineError message={subs.error} onRetry={reloadAll} />
          </div>
        ) : mounted && !connected ? (
          <div style={{ padding: 16 }}>
            <EmptyInline>Connect to the API (top bar) to load webhooks.</EmptyInline>
          </div>
        ) : subList.length === 0 ? (
          <div style={{ padding: 16 }}>
            <EmptyInline>No webhooks — add a subscription with + New subscription.</EmptyInline>
          </div>
        ) : (
          subList.map((s) => (
            <SubscriptionRow
              key={s.subscription_id}
              sub={s}
              deadLetters={dlqBySub.get(s.subscription_id) ?? []}
              dlqUnavailable={dlq.error != null}
              onChanged={reloadAll}
            />
          ))
        )}
      </Card>

      {formOpen && (
        <div style={{ marginTop: 16 }}>
          <SubscriptionForm onAdded={onAdded} onCancel={() => setFormOpen(false)} />
        </div>
      )}
    </section>
  );
}

function SubscriptionRow({
  sub: s,
  deadLetters,
  dlqUnavailable,
  onChanged,
}: {
  sub: WebhookSubscription;
  deadLetters: DeadLetter[];
  dlqUnavailable: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [replaying, setReplaying] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const n = deadLetters.length;

  async function doReplay() {
    setReplaying(true);
    try {
      await Promise.all(deadLetters.map((d) => replayDeadLetter(d.dead_letter_id)));
      toast(`Replayed ${n} dead-letter${n === 1 ? "" : "s"}`);
      onChanged();
    } catch (e) {
      toast(`Replay failed: ${errMsg(e)}`);
      setReplaying(false);
    }
  }

  async function doDelete() {
    if (
      typeof window !== "undefined" &&
      !window.confirm(`Delete webhook subscription to ${s.target_url}?`)
    ) {
      return;
    }
    setDeleting(true);
    try {
      await deleteWebhookSubscription(s.subscription_id);
      toast(`Deleted webhook subscription`);
      onChanged();
    } catch (e) {
      toast(`Delete failed: ${errMsg(e)}`);
      setDeleting(false);
    }
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "12px 16px",
        borderBottom: "1px solid var(--hair)",
      }}
    >
      {/* event chips */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 5, flexShrink: 0, maxWidth: 260 }}>
        {s.event_types.length === 0 ? (
          <EventChip>*</EventChip>
        ) : (
          s.event_types.map((ev) => <EventChip key={ev}>{ev}</EventChip>)
        )}
      </div>

      {/* target url */}
      <span
        title={s.target_url}
        style={{
          flex: 1,
          minWidth: 0,
          fontFamily: "var(--font-mono)",
          fontSize: 11.5,
          color: "var(--text-secondary)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {s.target_url}
      </span>

      {/* status */}
      <div style={{ flexShrink: 0 }}>
        {!s.active ? (
          <StatusChip tone="muted">inactive</StatusChip>
        ) : n > 0 ? (
          <StatusChip tone="warning">
            {n} dead-lettered
          </StatusChip>
        ) : dlqUnavailable ? (
          <StatusChip tone="muted">delivering</StatusChip>
        ) : (
          <StatusChip tone="success">delivering ✓</StatusChip>
        )}
      </div>

      {/* actions */}
      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
        {n > 0 && (
          <Button onClick={doReplay} disabled={replaying} style={{ padding: "5px 9px" }}>
            {replaying ? "Replaying…" : "Replay"}
          </Button>
        )}
        <Button
          variant="danger"
          onClick={doDelete}
          disabled={deleting}
          style={{ padding: "5px 9px" }}
        >
          {deleting ? "…" : "Delete"}
        </Button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Add-subscription form — CreateSubscriptionRequest is { deployment_ref,
// event_types[], target_url, tenant_id }. tenant_id defaults from the Connect
// bar's configured tenant.
// --------------------------------------------------------------------------

function SubscriptionForm({ onAdded, onCancel }: { onAdded: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [deploymentRef, setDeploymentRef] = useState("");
  const [events, setEvents] = useState("");
  const [targetUrl, setTargetUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const eventList = events
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const canSubmit =
    deploymentRef.trim().length > 0 &&
    targetUrl.trim().length > 0 &&
    eventList.length > 0 &&
    !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    try {
      await createWebhookSubscription({
        deployment_ref: deploymentRef.trim(),
        event_types: eventList,
        target_url: targetUrl.trim(),
        tenant_id: getTenant(),
      });
      toast(`Created webhook subscription`);
      onAdded();
    } catch (err) {
      toast(`Create failed: ${errMsg(err)}`);
      setBusy(false);
    }
  }

  return (
    <Card pad={16}>
      <div style={{ marginBottom: 4, fontSize: 15, fontWeight: 600 }}>New subscription</div>
      <p style={{ margin: "0 0 14px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.55 }}>
        Deliver deployment events to an HTTPS endpoint. A signing secret is generated server-side
        (never shown here).
      </p>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="deployment_ref" hint="The deployment whose events to deliver.">
          <TextInput
            value={deploymentRef}
            onChange={setDeploymentRef}
            placeholder="my-deployment"
            autoFocus
          />
        </Field>
        <Field label="event_types" hint="Comma-separated event names (e.g. run.succeeded, run.failed).">
          <TextInput value={events} onChange={setEvents} placeholder="run.succeeded, run.failed" />
        </Field>
        <Field label="target_url" hint="HTTPS endpoint that receives the signed POST.">
          <TextInput value={targetUrl} onChange={setTargetUrl} placeholder="https://example.com/hook" />
        </Field>
        <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
          <Button type="submit" variant="primary" disabled={!canSubmit}>
            {busy ? "Creating…" : "Create subscription"}
          </Button>
          <Button type="button" variant="neutral" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirrors the Templates / Deployments screen conventions)
// --------------------------------------------------------------------------

function SectionHead({ label, children }: { label: string; children?: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
        marginBottom: 10,
      }}
    >
      <MonoLabel>{label}</MonoLabel>
      {children}
    </div>
  );
}

function ColHead({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-faint)",
      }}
    >
      {children}
    </span>
  );
}

function CellText({ children, mono = false }: { children: React.ReactNode; mono?: boolean }) {
  return (
    <span
      style={{
        display: "block",
        minWidth: 0,
        fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
        fontSize: 12,
        color: "var(--text-secondary)",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {children}
    </span>
  );
}

/** A small mono uppercase tag (source: env/runtime). */
function MiniTag({ tone, children }: { tone: string; children: React.ReactNode }) {
  const c = TONE[tone] ?? tone;
  return (
    <span
      style={{
        display: "inline-flex",
        alignSelf: "flex-start",
        fontFamily: "var(--font-mono)",
        fontSize: 9.5,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: c,
        background: `color-mix(in srgb, ${c} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${c} 28%, transparent)`,
        borderRadius: 4,
        padding: "1px 6px",
      }}
    >
      {children}
    </span>
  );
}

/** Status: a toned StatusDot + mono label (no uppercasing). */
function StatusChip({ tone, children }: { tone: string; children: React.ReactNode }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
      <StatusDot tone={tone} />
      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: TONE[tone] ?? tone }}>
        {children}
      </span>
    </span>
  );
}

/** Event chip — violet (var(--agent)) mono, not uppercased so event names read naturally. */
function EventChip({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        color: "var(--agent)",
        background: "color-mix(in srgb, var(--agent) 12%, transparent)",
        border: "1px solid color-mix(in srgb, var(--agent) 30%, transparent)",
        borderRadius: 5,
        padding: "2px 7px",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "block" }}>
      <MonoLabel style={{ display: "block", marginBottom: 5 }}>{label}</MonoLabel>
      {children}
      {hint && (
        <span
          style={{
            display: "block",
            marginTop: 5,
            fontSize: 11,
            color: "var(--text-faint)",
            lineHeight: 1.5,
          }}
        >
          {hint}
        </span>
      )}
    </label>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  autoFocus,
  type = "text",
  inputMode,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  type?: "text" | "password";
  inputMode?: "numeric";
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      type={type}
      inputMode={inputMode}
      autoComplete={type === "password" ? "new-password" : "off"}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
        outline: "none",
      }}
    />
  );
}

function SelectInput({
  value,
  onChange,
  children,
}: {
  value: string;
  onChange: (v: string) => void;
  children: React.ReactNode;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
        outline: "none",
      }}
    >
      {children}
    </select>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>{children}</div>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 8,
        padding: "10px 12px",
      }}
    >
      <span
        style={{
          fontSize: 12.5,
          color: "var(--danger)",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {message}
      </span>
      <Button variant="danger" onClick={onRetry} style={{ flexShrink: 0 }}>
        Retry
      </Button>
    </div>
  );
}
