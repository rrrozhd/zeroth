"use client";

import { useEffect, useState } from "react";
import { ConnectBar } from "@/app/components/ConnectBar";
import { ApiError, listWorkflows, type WorkflowSummary } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

export default function Home() {
  const [items, setItems] = useState<WorkflowSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Gate localStorage-derived UI until after mount to avoid hydration mismatch.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setItems(await listWorkflows());
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status || ""} ${e.message}`.trim() : String(e));
      setItems(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="mx-auto w-full max-w-4xl px-6 py-10">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Zeroth Console</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Operate and author your Zeroth multi-agent apps.
        </p>
      </header>

      <section className="mb-6">
        <ConnectBar onChange={() => setError(null)} />
      </section>

      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-lg font-medium">Workflows</h2>
          <button
            onClick={load}
            disabled={loading}
            className="rounded-md border border-zinc-300 px-3 py-1 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          >
            {loading ? "Loading…" : "Load workflows"}
          </button>
          {mounted && !isConfigured() && (
            <span className="text-xs text-amber-600">Set an API key above first.</span>
          )}
        </div>

        {error && (
          <div className="rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </div>
        )}

        {items && items.length === 0 && !error && (
          <p className="text-sm text-zinc-500">No workflows yet.</p>
        )}

        {items && items.length > 0 && (
          <ul className="divide-y divide-zinc-200 overflow-hidden rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {items.map((w) => (
              <li
                key={w.id}
                className="flex items-center justify-between bg-white px-4 py-3 dark:bg-zinc-950"
              >
                <div>
                  <div className="font-medium">{w.name}</div>
                  <div className="font-mono text-xs text-zinc-500">{w.id}</div>
                </div>
                <div className="flex items-center gap-3 text-xs text-zinc-500">
                  <span className="rounded bg-zinc-100 px-2 py-0.5 dark:bg-zinc-800">
                    {w.status}
                  </span>
                  <span>v{w.version}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
