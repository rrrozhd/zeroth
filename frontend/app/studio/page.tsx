"use client";

import Link from "next/link";
import { useState } from "react";
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

      <Card title="New workflow">
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
      {data && data.length === 0 && <Empty>No workflows yet.</Empty>}

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
