"use client";

// The client app shell: Sidebar + Topbar + Toast + Regulus context, wrapping the
// active route's `{children}`. Rendered by the server `layout.tsx`.
//
// Regulus mount state is probed once on mount and shared via context so the
// Sidebar can show/hide the Regulus nav group and screens can degrade gracefully.

import { useEffect, useState } from "react";
import { ToastProvider } from "./Toast";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { detectRegulus, type RegulusStatus } from "@/app/lib/regulus";
import { RegulusCtx } from "./regulusContext";

export function AppShell({ children }: { children: React.ReactNode }) {
  const [reg, setReg] = useState<RegulusStatus>("unknown");
  useEffect(() => {
    detectRegulus().then(setReg);
  }, []);

  return (
    <ToastProvider>
      <RegulusCtx.Provider value={reg}>
        <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
          <Sidebar />
          <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0 }}>
            <Topbar />
            <main
              id="main"
              tabIndex={-1}
              style={{ flex: 1, overflowY: "auto" }}
              className="z-fade"
            >
              {children}
            </main>
          </div>
        </div>
      </RegulusCtx.Provider>
    </ToastProvider>
  );
}
