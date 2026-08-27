"use client";

import { useEffect } from "react";

import { assignEvidenceIdentities } from "@/app/lib/evidence-identity";

export function EvidenceIdentityObserver({ pathname }: { pathname: string }) {
  useEffect(() => {
    const evidenceRoot = document.querySelector<HTMLElement>(".console-shell") ?? document.body;
    let scheduled = false;
    const assign = () => {
      scheduled = false;
      const result = assignEvidenceIdentities(evidenceRoot, pathname);
      document.documentElement.dataset.evidenceControlCount = String(result.controls.length);
      document.documentElement.dataset.evidenceIdentityErrors = JSON.stringify(result.errors);
    };
    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      queueMicrotask(assign);
    };
    assign();
    const observer = new MutationObserver(schedule);
    observer.observe(evidenceRoot, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: [
        "aria-label",
        "aria-labelledby",
        "data-evidence-id",
        "data-evidence-scope",
        "disabled",
        "href",
        "name",
        "placeholder",
        "role",
        "title",
      ],
    });
    return () => observer.disconnect();
  }, [pathname]);

  return null;
}
