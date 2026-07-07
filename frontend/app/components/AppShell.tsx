"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getApiBase, getApiKey, isConfigured, setConfig } from "@/app/lib/config";
import { getLastWorkflowId } from "@/app/lib/lastWorkflow";
import { getHealth } from "@/app/lib/api";
import { Button, Input, useConnected } from "@/app/components/ui";

// Studio right after Overview: authoring is the center of the product, so it
// gets the most prominent slot; the operate/govern views follow.
const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/studio", label: "Studio" },
  { href: "/runs", label: "Runs" },
  { href: "/approvals", label: "Approvals" },
  { href: "/audit", label: "Audit" },
  { href: "/cost", label: "Cost" },
  { href: "/guide", label: "Guide" },
];

export function Header() {
  const pathname = usePathname();
  const active = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  // Studio deep-links to the last-edited workflow. Resolved after mount (and
  // re-resolved on every route change) so server HTML and the first client
  // render agree on href="/studio" — reading localStorage during render would
  // be a hydration mismatch.
  const [studioHref, setStudioHref] = useState("/studio");
  useEffect(() => {
    const last = getLastWorkflowId();
    setStudioHref(last ? `/studio/edit?id=${encodeURIComponent(last)}` : "/studio");
  }, [pathname]);

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-surface/80 backdrop-blur">
      {/* overflow-x-auto must stay on the nav only: putting it on this row
          clips the Connect popover to the 56px header strip (and focusing the
          popover's first field then scrolls the nav out of view). */}
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-4 px-6">
        <Link href="/" className="flex shrink-0 items-center gap-2">
          {/* Brand mark (circled dot); two-tone per color scheme, matching
              docs/assets/logo/zeroth-mark*.svg. */}
          <svg viewBox="0 0 100 100" fill="none" role="img" aria-label="Zeroth" className="h-6 w-6">
            <circle
              cx="50"
              cy="50"
              r="34"
              strokeWidth="10"
              className="stroke-[#3d5dd1] dark:stroke-[#7091ec]"
            />
            <circle cx="50" cy="50" r="9" className="fill-[#3ba3d7] dark:fill-[#74c2e8]" />
          </svg>
          <span className="text-sm font-semibold tracking-tight">zeroth</span>
        </Link>

        <nav aria-label="Primary" className="flex min-w-0 items-center gap-0.5 overflow-x-auto">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href === "/studio" ? studioHref : l.href}
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
  const connected = useConnected();
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
      {/* Primary CTA only while unconfigured — once connected it's a settings affordance. */}
      <Button
        ref={triggerRef}
        size="sm"
        variant={open || connected ? "default" : "primary"}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((o) => !o)}
      >
        {connected ? "Connection" : "Connect"}
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
