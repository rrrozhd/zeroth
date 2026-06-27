"use client";

import { useEffect, useState } from "react";
import { getApiBase, getApiKey, setConfig } from "@/app/lib/config";

/**
 * Connection settings: API base URL + API key, persisted to localStorage.
 * Leave the base blank when the console is mounted by the Zeroth app (requests
 * go same-origin); set it to point a standalone console at a remote API.
 */
export function ConnectBar({ onChange }: { onChange?: () => void }) {
  const [base, setBase] = useState("");
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setBase(getApiBase());
    setKey(getApiKey());
  }, []);

  function save(e: React.FormEvent) {
    e.preventDefault();
    setConfig(base, key);
    setSaved(true);
    onChange?.();
    window.setTimeout(() => setSaved(false), 1500);
  }

  return (
    <form
      onSubmit={save}
      className="flex flex-wrap items-end gap-3 rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-950"
    >
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium text-zinc-700 dark:text-zinc-300">
          API base URL{" "}
          <span className="font-normal text-zinc-400">(blank = same origin)</span>
        </span>
        <input
          value={base}
          onChange={(e) => setBase(e.target.value)}
          placeholder="https://api.example.com"
          className="w-72 rounded-md border border-zinc-300 px-3 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium text-zinc-700 dark:text-zinc-300">API key</span>
        <input
          value={key}
          onChange={(e) => setKey(e.target.value)}
          type="password"
          placeholder="X-API-Key"
          className="w-64 rounded-md border border-zinc-300 px-3 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
      </label>
      <button
        type="submit"
        className="rounded-md bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        {saved ? "Saved ✓" : "Save"}
      </button>
    </form>
  );
}
