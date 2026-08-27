"use client";

// The client app shell: Sidebar + Topbar + Toast + Regulus context, wrapping the
// active route's `{children}`. Rendered by the server `layout.tsx`.
//
// Regulus mount state is probed once on mount and shared via context so the
// Sidebar can show/hide the Regulus nav group and screens can degrade gracefully.

import { useCallback, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { ToastProvider } from "./Toast";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { detectRegulus, type RegulusStatus } from "@/app/lib/regulus";
import { RegulusCtx } from "./regulusContext";
import { usePolling } from "@/app/hooks/usePolling";
import { listApprovals } from "@/app/lib/api";
import { AuditVerificationCtx } from "./auditVerificationContext";
import { SidebarToggleButton } from "./SidebarToggleButton";
import { EvidenceIdentityObserver } from "./EvidenceIdentityObserver";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "";
  const editorChrome = pathname.startsWith("/studio/edit");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [reg, setReg] = useState<RegulusStatus>("unknown");
  const [auditVerifiedAt, setAuditVerifiedAt] = useState<string | null>(null);
  useEffect(() => {
    detectRegulus().then(setReg);
  }, []);

  useEffect(() => {
    const editorViewport = window.matchMedia("(max-width: 900px)");
    const phoneViewport = window.matchMedia("(max-width: 560px)");
    const syncToViewport = () => {
      const stored = window.localStorage.getItem("zeroth.sidebarCollapsed");
      const compactEditor = editorChrome && editorViewport.matches;
      setSidebarCollapsed(phoneViewport.matches || compactEditor || stored === "true");
    };
    const subscribe = (query: MediaQueryList) => {
      if (typeof query.addEventListener === "function") {
        query.addEventListener("change", syncToViewport);
        return () => query.removeEventListener("change", syncToViewport);
      }
      if (typeof query.addListener === "function") {
        query.addListener(syncToViewport);
        return () => query.removeListener(syncToViewport);
      }
      return () => undefined;
    };

    syncToViewport();
    const unsubscribeEditor = subscribe(editorViewport);
    const unsubscribePhone = subscribe(phoneViewport);
    return () => {
      unsubscribeEditor();
      unsubscribePhone();
    };
  }, [editorChrome]);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem("zeroth.sidebarCollapsed", String(next));
      return next;
    });
  }, []);

  useEffect(() => {
    if (!editorChrome) return;

    function onKeyDown(event: KeyboardEvent) {
      const primaryModifier = event.ctrlKey || event.metaKey;
      if (
        event.repeat ||
        !primaryModifier ||
        event.shiftKey ||
        event.altKey ||
        event.key.toLowerCase() !== "b"
      ) {
        return;
      }

      const target = event.target as HTMLElement | null;
      if (target?.isContentEditable) return;

      event.preventDefault();
      toggleSidebar();
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [editorChrome, toggleSidebar]);

  // Pending-approvals count for the sidebar badge: fetched once on mount and on a
  // slow poll. Any failure (unconfigured/unreachable API) degrades to 0.
  const [pendingApprovals, setPendingApprovals] = useState(0);
  usePolling(
    () => {
      listApprovals()
        .then((list) =>
          setPendingApprovals(list.filter((a) => (a.status ?? "pending") === "pending").length),
        )
        .catch(() => setPendingApprovals(0));
    },
    15000,
    true,
  );

  return (
    <ToastProvider>
      <EvidenceIdentityObserver pathname={pathname} />
      <RegulusCtx.Provider value={reg}>
        <AuditVerificationCtx.Provider
          value={{ verifiedAt: auditVerifiedAt, markVerified: setAuditVerifiedAt }}
        >
          <div
            className={`console-shell${editorChrome ? " is-editor-shell" : ""}${sidebarCollapsed ? " is-sidebar-collapsed" : ""}`}
            style={
              {
                display: "flex",
                height: "100vh",
                overflow: "hidden",
                "--console-sidebar-width": sidebarCollapsed ? "0px" : "216px",
              } as React.CSSProperties
            }
          >
            <Sidebar
              collapsed={sidebarCollapsed}
              pendingApprovals={pendingApprovals}
            />
            <div className="console-content">
              {editorChrome ? (
                <div className="console-editor-sidebar-control">
                  <SidebarToggleButton
                    collapsed={sidebarCollapsed}
                    onToggle={toggleSidebar}
                  />
                </div>
              ) : (
                <Topbar
                  sidebarCollapsed={sidebarCollapsed}
                  onSidebarToggle={toggleSidebar}
                />
              )}
              <main
                id="main"
                tabIndex={-1}
                style={{
                  flex: 1,
                  overflowY: "auto",
                  background: "var(--bg-page)",
                }}
              >
                {children}
              </main>
            </div>
          </div>
        </AuditVerificationCtx.Provider>
      </RegulusCtx.Provider>
    </ToastProvider>
  );
}
