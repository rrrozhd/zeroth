"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getApiBase, getApiKey, isConfigured, setConfig } from "@/app/lib/config";
import { getHealth } from "@/app/lib/api";
import { Button, Input } from "@/app/components/ui";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/runs", label: "Runs" },
  { href: "/approvals", label: "Approvals" },
  { href: "/audit", label: "Audit" },
  { href: "/cost", label: "Cost" },
  { href: "/studio", label: "Studio" },
];

export function Header() {
  const pathname = usePathname();
  const active = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-surface/80 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 overflow-x-auto px-6">
        <Link href="/" className="flex shrink-0 items-center gap-2">
          <span className="grid h-6 w-6 place-items-center rounded-md bg-accent text-[13px] font-bold text-accent-fg">
            0
          </span>
          <span className="text-sm font-semibold tracking-tight">Zeroth</span>
        </Link>

        <nav aria-label="Primary" className="flex shrink-0 items-center gap-0.5">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active(l.href) ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
                active(l.href)
                  ? "bg-accent/10 font-medium text-accent"
                  : "text-muted hover:bg-zinc-100 hover:text-foreground dark:hover:bg-zinc-800/60"
              }`}
            >
              {l.label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          <DeploymentChip />
          <ConnectPopover />
        </div>
      </div>
    </header>
  );
}

function DeploymentChip() {
  const [text, setText] = useState<string | null>(null);
  const [ok, setOk] = useState(false);

  useEffect(() => {
    let alive = true;
    if (!isConfigured()) {
      setText("Not connected");
      setOk(false);
      return;
    }
    getHealth()
      .then((h) => alive && (setText(h.deployment_ref), setOk(true)))
      .catch(() => alive && (setText("Unreachable"), setOk(false)));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <span className="hidden items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-xs text-muted lg:inline-flex">
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-zinc-400"}`}
      />
      <span className="font-mono">{text ?? "…"}</span>
    </span>
  );
}

function ConnectPopover() {
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState("");
  const [key, setKey] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  function close() {
    setOpen(false);
    triggerRef.current?.focus();
  }

  useEffect(() => {
    if (!open) return;
    setBase(getApiBase());
    setKey(getApiKey());
    firstFieldRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function save(e: React.FormEvent) {
    e.preventDefault();
    setConfig(base, key);
    // Reload so the new connection is picked up everywhere consistently.
    window.location.reload();
  }

  return (
    <div className="relative" ref={ref}>
      <Button
        ref={triggerRef}
        size="sm"
        variant={open ? "default" : "primary"}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((o) => !o)}
      >
        Connect
      </Button>
      {open && (
        <form
          role="dialog"
          aria-label="API connection settings"
          onSubmit={save}
          className="absolute right-0 top-full z-30 mt-2 w-80 space-y-3 rounded-xl border border-border bg-surface p-4 shadow-lg shadow-black/10"
        >
          <div className="text-sm font-semibold">Connection</div>
          <label className="block text-xs">
            <span className="mb-1 block text-muted">
              API base URL <span className="text-zinc-500">(blank = same origin)</span>
            </span>
            <Input
              ref={firstFieldRef}
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder="https://api.example.com"
              className="font-mono"
            />
          </label>
          <label className="block text-xs">
            <span className="mb-1 block text-muted">API key</span>
            <Input
              value={key}
              onChange={(e) => setKey(e.target.value)}
              type="password"
              placeholder="sk-… / your operator key"
              className="font-mono"
            />
            <span className="mt-1 block text-[11px] text-muted">
              Sent as the <code>X-API-Key</code> header.
            </span>
          </label>
          <Button type="submit" variant="primary" size="sm" className="w-full">
            Save &amp; reload
          </Button>
        </form>
      )}
    </div>
  );
}
