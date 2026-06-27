"use client";

// Placeholder for the Studio graph canvas (M4). Replaced by the React Flow
// editor in the next milestone.
import Link from "next/link";

export default function StudioEditPlaceholder() {
  return (
    <div className="space-y-4">
      <Link href="/studio" className="text-sm text-zinc-500 hover:underline">
        ← Back to Studio
      </Link>
      <h1 className="text-2xl font-semibold tracking-tight">Workflow editor</h1>
      <p className="text-sm text-zinc-500">The graph canvas is coming in the next milestone.</p>
    </div>
  );
}
