"use client";

// The Studio list screen — author and manage workflow graphs, wired to the live
// Studio API. Restyled onto the P0 dark/teal design system (tokens + primitives);
// the data flow is unchanged: graphs come from `listWorkflows`, a row opens the
// canvas at `/studio/edit?id=…`, "Start from scratch" creates a draft via
// `createWorkflow`, and each template instantiates an editable draft via
// `instantiateTemplate`. The API key lives only in localStorage — never logged,
// never placed in a URL (rows carry the workflow id alone).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { NodeGlyph } from "@/app/components/nodeMeta";
import { Button, Card, MonoLabel, NODE_TYPE_COLOR, Pill, Skeleton } from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  createWorkflow,
  deleteWorkflow,
  errMsg,
  listWorkflows,
  type WorkflowSummary,
} from "@/app/lib/api";
import {
  instantiateTemplate,
  WORKFLOW_TEMPLATES,
  type WorkflowTemplate,
} from "@/app/lib/templates";

const MONO = "var(--font-mono)";

// Backend node_type discriminator -> the P0 NODE_TYPE_COLOR shorthand key, so a
// template's step squares carry each node type's own accent color.
const NODE_TYPE_KEY: Record<string, string> = {
  entrypoint: "entrypoint",
  agent: "agent",
  retrieval: "retrieval",
  human_approval: "approval",
  executable_unit: "exec",
  code: "exec",
  subgraph: "subgraph",
};

function nodeColor(type: string): string {
  return NODE_TYPE_COLOR[NODE_TYPE_KEY[type] ?? "subgraph"] ?? "var(--nt-subgraph)";
}

// Graph lifecycle -> its state color (handoff §2): deployed teal, published
// green, draft muted; anything unmapped degrades to muted.
const LIFECYCLE_COLOR: Record<string, string> = {
  deployed: "var(--accent)",
  published: "var(--success)",
  draft: "var(--text-muted)",
};

function lifecycleColor(status: string): string {
  return LIFECYCLE_COLOR[status?.toLowerCase()] ?? "var(--text-muted)";
}

export default function StudioPage() {
  const toast = useToast();
  const { data, error, loading, reload } = useLoad<WorkflowSummary[]>(listWorkflows);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; msg: string } | null>(null);

  async function create() {
    if (!name.trim()) return;
    const label = name.trim();
    setBusy(true);
    setFormError(null);
    try {
      await createWorkflow(label);
      setName("");
      toast(`Created draft "${label}"`);
      reload();
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, label: string) {
    if (!window.confirm(`Delete "${label}"? This cannot be undone.`)) return;
    setDeletingId(id);
    setRowError(null);
    try {
      await deleteWorkflow(id);
      toast(`Deleted "${label}"`);
      reload();
    } catch (e) {
      setRowError({ id, msg: errMsg(e) });
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "flex-end",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 22,
        }}
      >
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Studio</h1>
          <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
            Author and manage workflow graphs.
          </p>
        </div>
        <Button variant="neutral" onClick={() => reload()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      </header>

      {/* Graph list — the primary content of the Studio page. */}
      <section style={{ marginBottom: 26 }}>
        <MonoLabel style={{ display: "block", marginBottom: 10 }}>Your graphs</MonoLabel>

        {error && <ApiErrorNote error={error} />}
        {loading && !data && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <Skeleton height={44} />
            <Skeleton height={44} />
            <Skeleton height={44} />
          </div>
        )}
        {data && data.length === 0 && (
          <EmptyNote>No workflows yet — pick a template below, or create a blank one.</EmptyNote>
        )}

        {data && data.length > 0 && (
          <div
            style={{
              border: "1px solid var(--hair)",
              borderRadius: 8,
              overflow: "hidden",
              background: "var(--bg-card)",
            }}
          >
            {data.map((w, i) => (
              <GraphRow
                key={w.id}
                w={w}
                last={i === data.length - 1}
                deleting={deletingId === w.id}
                rowError={rowError?.id === w.id ? rowError.msg : null}
                onDelete={() => remove(w.id, w.name)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Start something new. */}
      <TemplateGallery />

      <section style={{ marginTop: 22 }}>
        <Card label="Start from scratch">
          <div style={{ display: "flex", alignItems: "flex-end", gap: 12 }}>
            <label style={{ flex: 1, minWidth: 0 }}>
              <MonoLabel style={{ display: "block", marginBottom: 6 }}>New graph name</MonoLabel>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && create()}
                placeholder="My workflow"
                style={{
                  width: "100%",
                  boxSizing: "border-box",
                  fontFamily: MONO,
                  fontSize: 12.5,
                  color: "var(--text-primary)",
                  background: "var(--bg-code)",
                  border: "1px solid var(--hair-strong)",
                  borderRadius: 6,
                  padding: "8px 10px",
                  outline: "none",
                }}
              />
            </label>
            <Button variant="primary" onClick={create} disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create"}
            </Button>
          </div>
          {formError && (
            <div style={{ marginTop: 12 }}>
              <ErrorBox message={formError} />
            </div>
          )}
        </Card>
      </section>
    </div>
  );
}

// A single graph row: mono name + id (opens the canvas), colored uppercase
// lifecycle state, version, and Edit / Delete controls. Hover paints the teal
// tint + accent left edge from handoff §2.
function GraphRow({
  w,
  last,
  deleting,
  rowError,
  onDelete,
}: {
  w: WorkflowSummary;
  last: boolean;
  deleting: boolean;
  rowError: string | null;
  onDelete: () => void;
}) {
  const [hover, setHover] = useState(false);
  const href = `/studio/edit?id=${encodeURIComponent(w.id)}`;
  const stateColor = lifecycleColor(w.status);
  const canDelete = w.status !== "published";

  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        borderBottom: last ? "none" : "1px solid var(--hair)",
        borderLeft: `2px solid ${hover ? "var(--accent)" : "transparent"}`,
        background: hover ? "rgba(94,234,212,0.06)" : "transparent",
        transition: "background 120ms ease, border-color 120ms ease",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 14px" }}>
        <Link
          href={href}
          style={{ minWidth: 0, flex: 1, textDecoration: "none", color: "inherit" }}
        >
          <div
            style={{
              fontFamily: MONO,
              fontSize: 13,
              color: hover ? "var(--accent)" : "var(--text-primary)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              transition: "color 120ms ease",
            }}
          >
            {w.name}
          </div>
          <div
            style={{
              marginTop: 2,
              fontFamily: MONO,
              fontSize: 11,
              color: "var(--text-faint)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {w.id}
          </div>
        </Link>

        <span
          style={{
            fontFamily: MONO,
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            color: stateColor,
            flexShrink: 0,
          }}
        >
          {w.status}
        </span>
        <Pill tone="neutral" style={{ flexShrink: 0 }}>
          v{w.version}
        </Pill>
        <Link href={href} style={{ flexShrink: 0, textDecoration: "none" }}>
          <Button variant="neutral">Edit</Button>
        </Link>
        {canDelete && (
          <Button
            variant="danger"
            onClick={onDelete}
            disabled={deleting}
            style={{ flexShrink: 0 }}
          >
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        )}
      </div>
      {rowError && (
        <div style={{ padding: "0 14px 11px" }}>
          <ErrorBox message={rowError} />
        </div>
      )}
    </div>
  );
}

// Ready-made example graphs so a first workflow doesn't start from a blank
// canvas. Instantiating creates a normal draft and jumps straight to the editor.
function TemplateGallery() {
  const router = useRouter();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function use(t: WorkflowTemplate) {
    setBusyId(t.id);
    setError(null);
    try {
      const wf = await instantiateTemplate(t);
      router.push(`/studio/edit?id=${encodeURIComponent(wf.id)}`);
    } catch (e) {
      setError(errMsg(e));
      setBusyId(null);
    }
  }

  return (
    <section>
      <MonoLabel style={{ display: "block", marginBottom: 10 }}>Start from a template</MonoLabel>
      {error && (
        <div style={{ marginBottom: 12 }}>
          <ErrorBox message={error} />
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
        {WORKFLOW_TEMPLATES.map((t) => (
          <TemplateCard
            key={t.id}
            template={t}
            busy={busyId !== null}
            busyThis={busyId === t.id}
            onUse={() => use(t)}
          />
        ))}
      </div>
    </section>
  );
}

// A tappable template card: node-type accent squares for each step, name,
// description, and a "Use template" affordance. The whole card is the button.
function TemplateCard({
  template: t,
  busy,
  busyThis,
  onUse,
}: {
  template: WorkflowTemplate;
  busy: boolean;
  busyThis: boolean;
  onUse: () => void;
}) {
  const [hover, setHover] = useState(false);
  const active = hover && !busy;

  return (
    <button
      type="button"
      onClick={onUse}
      disabled={busy}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex",
        flexDirection: "column",
        textAlign: "left",
        width: "100%",
        background: active ? "rgba(94,234,212,0.04)" : "var(--bg-card)",
        border: `1px solid ${active ? "rgba(94,234,212,0.4)" : "var(--hair)"}`,
        borderRadius: 8,
        padding: 16,
        cursor: busy ? "default" : "pointer",
        opacity: busy && !busyThis ? 0.55 : 1,
        transition: "background 120ms ease, border-color 120ms ease, opacity 120ms ease",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }} aria-label={t.tagline}>
        {t.nodes.map((n, i) => (
          <span key={n.id} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {i > 0 && (
              <span aria-hidden style={{ fontSize: 11, color: "var(--text-faint)" }}>
                →
              </span>
            )}
            <span
              title={String((n.data as { label?: string }).label ?? n.type)}
              style={{
                display: "grid",
                placeItems: "center",
                width: 26,
                height: 26,
                borderRadius: 6,
                flexShrink: 0,
                color: nodeColor(n.type),
                background: `color-mix(in srgb, ${nodeColor(n.type)} 14%, transparent)`,
              }}
            >
              <NodeGlyph type={n.type} className="h-3.5 w-3.5" />
            </span>
          </span>
        ))}
      </div>
      <div style={{ marginTop: 12, fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
        {t.name}
      </div>
      <p style={{ marginTop: 4, flex: 1, fontSize: 12, lineHeight: 1.6, color: "var(--text-muted)" }}>
        {t.description}
      </p>
      <div
        style={{
          marginTop: 12,
          fontFamily: MONO,
          fontSize: 11.5,
          fontWeight: 500,
          color: "var(--accent)",
        }}
      >
        {busyThis ? "Creating…" : "Use template →"}
      </div>
    </button>
  );
}

// A dashed, muted empty-state note.
function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <p
      style={{
        border: "1px dashed var(--hair-strong)",
        borderRadius: 8,
        padding: "22px 16px",
        textAlign: "center",
        fontSize: 13,
        color: "var(--text-muted)",
      }}
    >
      {children}
    </p>
  );
}

// A red-tinted inline error box (form + row failures).
function ErrorBox({ message }: { message: string }) {
  return (
    <div
      style={{
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 6,
        padding: "10px 12px",
        fontSize: 12.5,
        color: "var(--danger)",
      }}
    >
      {message}
    </div>
  );
}

// Friendly, code-aware copy for a load error (ported from the legacy ui.tsx
// ApiErrorNote so this page keeps the same UX without depending on ui.tsx).
function ApiErrorNote({ error }: { error: string }) {
  if (error.startsWith("403")) {
    return (
      <EmptyNote>
        Your API key doesn&apos;t have permission for this view — it requires an elevated role
        (e.g. admin/auditor).
      </EmptyNote>
    );
  }
  if (error.startsWith("401")) {
    return <EmptyNote>Not authenticated. Check your API key via Connect.</EmptyNote>;
  }
  if (error.startsWith("0 ") || error.toLowerCase().includes("network error")) {
    return (
      <EmptyNote>Can&apos;t reach the API. Check the base URL and that the service is running.</EmptyNote>
    );
  }
  if (error.startsWith("503")) {
    return <EmptyNote>This feature isn&apos;t configured for the deployment.</EmptyNote>;
  }
  return <ErrorBox message={error} />;
}
