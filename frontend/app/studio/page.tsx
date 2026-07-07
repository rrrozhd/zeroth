"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { NodeGlyph } from "@/app/components/nodeMeta";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  Input,
  PageHeader,
  Skeleton,
  StatusBadge,
  useAsync,
} from "@/app/components/ui";
import { createWorkflow, deleteWorkflow, errMsg, listWorkflows } from "@/app/lib/api";
import {
  instantiateTemplate,
  WORKFLOW_TEMPLATES,
  type WorkflowTemplate,
} from "@/app/lib/templates";

export default function StudioPage() {
  const { data, error, loading, reload } = useAsync(listWorkflows, []);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; msg: string } | null>(null);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setFormError(null);
    try {
      await createWorkflow(name.trim());
      setName("");
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
      reload();
    } catch (e) {
      setRowError({ id, msg: errMsg(e) });
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Studio"
        subtitle="Author and manage workflow graphs."
        actions={
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />

      <TemplateGallery />

      <Card title="Start from scratch">
        <div className="flex items-end gap-3">
          <div className="flex-1">
            <Field label="Name">
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && create()}
                placeholder="My workflow"
              />
            </Field>
          </div>
          <Button variant="primary" onClick={create} disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </div>
        {formError && (
          <div className="mt-3">
            <ErrorBox message={formError} />
          </div>
        )}
      </Card>

      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={4} />}
      {data && data.length === 0 && (
        <Empty>No workflows yet — pick a template above, or create a blank one.</Empty>
      )}

      {data && data.length > 0 && (
        <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface">
          {data.map((w) => (
            <li key={w.id} className="px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <Link
                    href={`/studio/edit?id=${encodeURIComponent(w.id)}`}
                    className="font-medium hover:text-accent"
                  >
                    {w.name}
                  </Link>
                  <div className="truncate font-mono text-xs text-muted">{w.id}</div>
                </div>
                <div className="flex shrink-0 items-center gap-3 text-xs text-muted">
                  <StatusBadge status={w.status} />
                  <span>v{w.version}</span>
                  <Link href={`/studio/edit?id=${encodeURIComponent(w.id)}`}>
                    <Button size="sm">Edit</Button>
                  </Link>
                  {w.status !== "published" && (
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => remove(w.id, w.name)}
                      disabled={deletingId === w.id}
                    >
                      {deletingId === w.id ? "Deleting…" : "Delete"}
                    </Button>
                  )}
                </div>
              </div>
              {rowError?.id === w.id && (
                <div className="mt-2">
                  <ErrorBox message={rowError.msg} />
                </div>
              )}
            </li>
          ))}
        </ul>
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
    <div>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Start from a template
      </div>
      {error && (
        <div className="mb-3">
          <ErrorBox message={error} />
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        {WORKFLOW_TEMPLATES.map((t) => (
          <div
            key={t.id}
            className="flex flex-col rounded-xl border border-border bg-surface p-4 shadow-sm shadow-black/[0.03]"
          >
            <div className="flex items-center gap-1.5" aria-label={t.tagline}>
              {t.nodes.map((n, i) => (
                <span key={n.id} className="flex items-center gap-1.5">
                  {i > 0 && (
                    <span aria-hidden className="text-xs text-muted">
                      →
                    </span>
                  )}
                  <span
                    title={String((n.data as { label?: string }).label ?? n.type)}
                    className="grid h-7 w-7 place-items-center rounded-md bg-accent/10 text-accent"
                  >
                    <NodeGlyph type={n.type} className="h-3.5 w-3.5" />
                  </span>
                </span>
              ))}
            </div>
            <div className="mt-3 font-medium">{t.name}</div>
            <p className="mt-1 flex-1 text-xs leading-relaxed text-muted">{t.description}</p>
            <div className="mt-3">
              <Button size="sm" onClick={() => use(t)} disabled={busyId !== null}>
                {busyId === t.id ? "Creating…" : "Use template"}
              </Button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
