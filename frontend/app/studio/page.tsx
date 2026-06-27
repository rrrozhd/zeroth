"use client";

import Link from "next/link";
import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  Input,
  PageHeader,
  StatusBadge,
  useAsync,
} from "@/app/components/ui";
import { createWorkflow, deleteWorkflow, errMsg, listWorkflows } from "@/app/lib/api";

export default function StudioPage() {
  const { data, error, loading, reload } = useAsync(listWorkflows, []);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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

  async function remove(id: string) {
    try {
      await deleteWorkflow(id);
      reload();
    } catch (e) {
      setFormError(errMsg(e));
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Studio"
        subtitle="Author and manage workflow graphs."
        actions={
          <Button onClick={reload} disabled={loading}>
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

      {error && <ErrorBox message={error} />}
      {data && data.length === 0 && <Empty>No workflows yet.</Empty>}

      {data && data.length > 0 && (
        <ul className="divide-y divide-zinc-200 overflow-hidden rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
          {data.map((w) => (
            <li
              key={w.id}
              className="flex items-center justify-between bg-white px-4 py-3 dark:bg-zinc-950"
            >
              <div>
                <Link
                  href={`/studio/edit?id=${encodeURIComponent(w.id)}`}
                  className="font-medium hover:underline"
                >
                  {w.name}
                </Link>
                <div className="font-mono text-xs text-zinc-500">{w.id}</div>
              </div>
              <div className="flex items-center gap-3 text-xs text-zinc-500">
                <StatusBadge status={w.status} />
                <span>v{w.version}</span>
                <Link
                  href={`/studio/edit?id=${encodeURIComponent(w.id)}`}
                  className="rounded-md border border-zinc-300 px-2 py-1 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
                >
                  Edit
                </Link>
                <Button variant="danger" onClick={() => remove(w.id)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
