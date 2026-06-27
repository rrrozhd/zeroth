"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// basePath (/console) is applied automatically by next/link and usePathname,
// so hrefs here are written WITHOUT the /console prefix.
const LINKS: { href: string; label: string }[] = [
  { href: "/", label: "Overview" },
  { href: "/runs", label: "Runs" },
  { href: "/approvals", label: "Approvals" },
  { href: "/audit", label: "Audit" },
  { href: "/cost", label: "Cost" },
  { href: "/studio", label: "Studio" },
];

export function Nav() {
  const pathname = usePathname();
  const active = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <nav className="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      <div className="mx-auto flex max-w-5xl items-center gap-1 px-6">
        <Link href="/" className="mr-4 py-3 font-semibold tracking-tight">
          Zeroth&nbsp;Console
        </Link>
        {LINKS.map((l) => (
          <Link
            key={l.href}
            href={l.href}
            className={`border-b-2 px-3 py-3 text-sm transition-colors ${
              active(l.href)
                ? "border-zinc-900 font-medium text-zinc-900 dark:border-zinc-100 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
            }`}
          >
            {l.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
