"use client";

import { useCallback, useEffect, useState } from "react";
import { errMsg } from "@/app/lib/api";

// --- Async data hook: load on mount + manual refresh, with loading/error state.
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fn());
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { data, error, loading, reload: run, setData };
}

// --- Page header: consistent title / subtitle / actions row across pages.
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions}
    </header>
  );
}

export function Card({
  title,
  actions,
  children,
  className = "",
}: {
  title?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-border bg-surface shadow-sm shadow-black/[0.03] ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          {title && <h2 className="text-sm font-semibold">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const STATUS_TONES: Record<string, string> = {
  completed: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  succeeded: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  published: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  ok: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  running: "bg-blue-500/12 text-blue-700 dark:text-blue-400",
  pending: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  paused: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  awaiting_approval: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  draft: "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400",
  failed: "bg-red-500/12 text-red-700 dark:text-red-400",
  dead_letter: "bg-red-500/12 text-red-700 dark:text-red-400",
};

const DOT_TONES: Record<string, string> = {
  completed: "bg-emerald-500",
  succeeded: "bg-emerald-500",
  published: "bg-emerald-500",
  ok: "bg-emerald-500",
  running: "bg-blue-500",
  pending: "bg-amber-500",
  paused: "bg-amber-500",
  awaiting_approval: "bg-amber-500",
  draft: "bg-zinc-400",
  failed: "bg-red-500",
  dead_letter: "bg-red-500",
};

export function StatusBadge({ status, dot = true }: { status: string; dot?: boolean }) {
  const key = status?.toLowerCase();
  const tone = STATUS_TONES[key] ?? "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400";
  const dotTone = DOT_TONES[key] ?? "bg-zinc-400";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${tone}`}
    >
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${dotTone}`} />}
      {status}
    </span>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
      {message}
    </div>
  );
}

export function Button({
  children,
  variant = "default",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm" | "md";
}) {
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed";
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-1.5 text-sm" };
  const tones = {
    default:
      "border border-border bg-surface hover:bg-zinc-50 dark:hover:bg-zinc-800/60",
    primary: "bg-accent text-accent-fg hover:opacity-90 shadow-sm shadow-accent/25",
    danger:
      "border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-900/60 dark:text-red-400 dark:hover:bg-red-950/40",
    ghost: "text-muted hover:bg-zinc-100 hover:text-foreground dark:hover:bg-zinc-800/60",
  };
  return (
    <button className={`${base} ${sizes[size]} ${tones[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

const fieldInput =
  "w-full rounded-lg border border-border bg-surface px-3 py-1.5 text-sm placeholder:text-zinc-400 focus-visible:border-accent";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${fieldInput} ${props.className ?? ""}`} />;
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 flex items-baseline gap-2">
        <span className="font-medium">{label}</span>
        {hint && <span className="text-xs font-normal text-zinc-400">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-relaxed text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Mono({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-xs text-muted dark:bg-zinc-800">
      {children}
    </code>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
      {children}
    </p>
  );
}

/** Render an API error, with friendlier copy for common auth/config codes. */
export function ApiErrorNote({ error }: { error: string }) {
  if (error.startsWith("403")) {
    return (
      <Empty>
        Your API key doesn&apos;t have permission for this view — it requires an
        elevated role (e.g. admin/auditor).
      </Empty>
    );
  }
  return <ErrorBox message={error} />;
}

/** Read a single query-string param on the client (static-export-safe). */
export function useQueryParam(name: string): string | null {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    setValue(new URLSearchParams(window.location.search).get(name));
  }, [name]);
  return value;
}
