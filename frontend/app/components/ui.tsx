"use client";

import { forwardRef, useCallback, useEffect, useState } from "react";
import { errMsg } from "@/app/lib/api";
import { getApiBase, isConfigured } from "@/app/lib/config";

// --- Async data hook: load on mount + manual refresh, with loading/error state.
// `background` reloads (e.g. polling) don't toggle the visible loading flag.
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    setError(null);
    try {
      setData(await fn());
    } catch (e) {
      setError(errMsg(e));
    } finally {
      if (!background) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { data, error, loading, reload: run, setData };
}

/** True once mounted AND an API key is set — safe against hydration mismatch. */
export function useConnected(): boolean {
  const [connected, setConnected] = useState(false);
  useEffect(() => setConnected(isConfigured()), []);
  return connected;
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
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
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
          {title && <h2 className="text-base font-semibold">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const STATUS_TONES: Record<string, string> = {
  completed: "bg-raised text-success",
  succeeded: "bg-raised text-success",
  published: "bg-raised text-success",
  approved: "bg-raised text-success",
  resolved: "bg-raised text-success",
  ok: "bg-raised text-success",
  active: "bg-raised text-success",
  running: "bg-raised text-info",
  approval_api: "bg-raised text-info",
  pending: "bg-raised text-warning",
  queued: "bg-raised text-warning",
  paused: "bg-raised text-warning",
  paused_for_approval: "bg-raised text-warning",
  awaiting_approval: "bg-raised text-warning",
  waiting_interrupt: "bg-raised text-warning",
  escalated: "bg-raised text-warning",
  unavailable: "bg-raised text-warning",
  unauthenticated: "bg-raised text-warning",
  draft: "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400",
  superseded: "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400",
  cancelled: "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400",
  failed: "bg-raised text-danger",
  error: "bg-raised text-danger",
  forbidden: "bg-raised text-danger",
  rejected: "bg-raised text-danger",
  terminated_by_policy: "bg-raised text-danger",
  terminated_by_loop_guard: "bg-raised text-danger",
  dead_letter: "bg-raised text-danger",
};

const DOT_TONES: Record<string, string> = {
  completed: "bg-success",
  succeeded: "bg-success",
  published: "bg-success",
  approved: "bg-success",
  resolved: "bg-success",
  ok: "bg-success",
  active: "bg-success",
  running: "bg-info",
  approval_api: "bg-info",
  pending: "bg-warning",
  queued: "bg-warning",
  paused: "bg-warning",
  paused_for_approval: "bg-warning",
  awaiting_approval: "bg-warning",
  waiting_interrupt: "bg-warning",
  escalated: "bg-warning",
  unavailable: "bg-warning",
  unauthenticated: "bg-warning",
  draft: "bg-zinc-400",
  superseded: "bg-zinc-400",
  cancelled: "bg-zinc-400",
  failed: "bg-danger",
  error: "bg-danger",
  forbidden: "bg-danger",
  rejected: "bg-danger",
  terminated_by_policy: "bg-danger",
  terminated_by_loop_guard: "bg-danger",
  dead_letter: "bg-danger",
};

// Friendly labels for raw backend statuses that don't humanize cleanly.
const STATUS_LABELS: Record<string, string> = {
  approval_api: "Approval resolved",
  paused_for_approval: "Awaiting approval",
  waiting_interrupt: "Interrupting",
  terminated_by_policy: "Blocked by policy",
  terminated_by_loop_guard: "Stopped by loop guard",
  dead_letter: "Dead letter",
};

/** Humanize a snake_case status for display. */
export function humanize(s: string): string {
  return s.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function StatusBadge({ status, dot = true }: { status: string; dot?: boolean }) {
  const key = status?.toLowerCase();
  if (process.env.NODE_ENV !== "production" && key && !(key in STATUS_TONES)) {
    // A neutral badge on an unmapped status can make a serious state (e.g. a
    // policy kill) look harmless — surface it during development.
    console.warn(`StatusBadge: unmapped status "${status}" rendered with neutral tone`);
  }
  const tone = STATUS_TONES[key] ?? "bg-zinc-500/12 text-zinc-600 dark:text-zinc-400";
  const dotTone = DOT_TONES[key] ?? "bg-zinc-400";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium ${tone}`}
    >
      {dot && <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${dotTone}`} />}
      {STATUS_LABELS[key] ?? humanize(status)}
    </span>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="flex gap-2 rounded-lg border border-border bg-surface px-4 py-3 text-sm text-foreground">
      <strong className="shrink-0 text-danger">Error</strong>
      <span>{message}</span>
    </div>
  );
}

export const Button = forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: "default" | "primary" | "danger" | "ghost";
    size?: "sm" | "md";
  }
>(function Button({ children, variant = "default", size = "md", className = "", ...props }, ref) {
  const base =
    "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg font-medium transition-colors disabled:cursor-not-allowed";
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-1.5 text-sm" };
  const tones = {
    default:
      "border border-border bg-surface hover:bg-zinc-50 disabled:opacity-50 dark:hover:bg-zinc-800/60",
    // Disabled filled button must still read as disabled (opacity-50 on accent
    // drops contrast below ~1.6:1), so swap to a clear muted fill.
    primary:
      "bg-accent text-accent-fg shadow-sm shadow-accent/25 hover:opacity-90 disabled:bg-zinc-200 disabled:text-zinc-500 disabled:shadow-none dark:disabled:bg-zinc-800 dark:disabled:text-zinc-500",
    danger:
      "border border-border text-danger hover:bg-raised disabled:opacity-50",
    ghost:
      "text-muted hover:bg-zinc-100 hover:text-foreground disabled:opacity-50 dark:hover:bg-zinc-800/60",
  };
  return (
    <button ref={ref} className={`${base} ${sizes[size]} ${tones[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
});

export const fieldInput =
  "w-full rounded-lg border border-border bg-surface px-3 py-1.5 text-sm placeholder:text-zinc-500 focus-visible:border-accent dark:placeholder:text-zinc-500";

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input(props, ref) {
    return <input ref={ref} {...props} className={`${fieldInput} ${props.className ?? ""}`} />;
  },
);

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`${fieldInput} ${props.className ?? ""}`} />;
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
        {hint && <span className="text-xs font-normal text-muted">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-relaxed text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
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

/** Pulsing placeholder rows while a page's initial data is in flight. */
export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-hidden className="space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <div
          key={i}
          className="h-9 animate-pulse rounded-lg bg-zinc-100 dark:bg-zinc-800/60"
        />
      ))}
    </div>
  );
}

/** Local, readable timestamp; hover reveals the raw ISO value. */
export function fmtTime(iso: string | null | undefined): React.ReactNode {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return <span title={iso}>{d.toLocaleString()}</span>;
}

/** Dollar amounts: cent precision from $1 up, sub-cent precision below. */
export function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0.00";
  const sign = n < 0 ? "-" : "";
  const amount = Math.abs(n);
  const rendered = amount >= 0.01
    ? amount.toFixed(2)
    : amount >= 0.0001
      ? amount.toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
      : amount.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
  return `${sign}$${rendered}`;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg bg-raised px-4 py-4 text-center text-sm text-muted">
      {children}
    </p>
  );
}

/** Shown on data pages when no API key is set, instead of a confusing API error. */
export function NotConnected() {
  return (
    <Empty>
      Not connected. Use <span className="font-medium text-foreground">Connect</span> in the
      top right to set your API base URL and key.
    </Empty>
  );
}

/** Render an API error with friendly, code-aware copy. */
export function ApiErrorNote({ error }: { error: string }) {
  if (error.startsWith("403")) {
    return (
      <Empty>
        Your API key doesn&apos;t have permission for this view — it requires an elevated
        role (e.g. admin/auditor).
      </Empty>
    );
  }
  if (error.startsWith("401")) {
    return (
      <Empty>
        Not authenticated. Check your API key via <span className="font-medium text-foreground">Connect</span>.
      </Empty>
    );
  }
  if (error.startsWith("0 ") || error.toLowerCase().includes("network error")) {
    return (
      <Empty>
        Can&apos;t reach the API. Check the base URL and that the service is running.
      </Empty>
    );
  }
  if (error.startsWith("503")) {
    // e.g. cost: "Regulus backend not configured" — this is optional infra,
    // not a failure, so present it as an informational state.
    return (
      <Empty>
        This feature isn&apos;t configured for the deployment.
        {error.toLowerCase().includes("regulus") &&
          " Cost tracking needs the Regulus backend, which isn't wired up here."}
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

/** The ready-to-run curl for invoking the deployed graph as an API service,
    built from the console's live connection + the given payload. */
export function buildRunCurl(
  payloadJson: string,
  threadId?: string,
  campaignId?: string | null,
): string {
  const base =
    getApiBase() || (typeof window === "undefined" ? "$API_BASE" : window.location.origin);
  // Keep copied commands safe and portable. Operators provide the key through
  // the shell environment; the console credential never enters clipboard text.
  const key = "$ZEROTH_API_KEY";
  let payload = payloadJson.trim() || "{}";
  try {
    payload = JSON.stringify(JSON.parse(payload)); // compact if valid
  } catch {
    /* embed as typed — the API will report the parse error */
  }
  const thread = threadId?.trim()
    ? `, "thread_id": ${JSON.stringify(threadId.trim())}`
    : "";
  const campaign = campaignId?.trim()
    ? `, "campaign_id": ${JSON.stringify(campaignId.trim())}`
    : "";
  const data = `{"input_payload": ${payload}${thread}${campaign}}`.replaceAll("'", "'\\''");
  return [
    `curl -fsS -X POST "${base}/v1/runs" \\`,
    `  -H "X-API-Key: ${key}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${data}'`,
  ].join("\n");
}

/** Safe shell snippet with a copy button. Credentials stay environment-bound. */
export function CurlBlock({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(t);
  }, [copied]);

  return (
    <div className="space-y-1.5">
      <pre className="overflow-x-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-relaxed text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
        {command}
      </pre>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-muted">
          Uses the shell&apos;s ZEROTH_API_KEY; no credential is copied.
        </span>
        <Button
          type="button"
          size="sm"
          onClick={() => {
            navigator.clipboard?.writeText(command).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied ✓" : "Copy"}
        </Button>
      </div>
    </div>
  );
}
