"use client";

// The approved Console v2 navigation rail.
// Brand block at the top, grouped nav in the middle (the Regulus group appears
// only when the econ plane is actually mounted), and a connection footer that
// opens the Connect bar.

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV } from "./nav";
import { VERSION } from "@/app/lib/version";
import { getApiBase } from "@/app/lib/config";
import { useRegulus } from "./regulusContext";
import { ConnectBar } from "./ConnectBar";
import { StatusDot } from "./primitives";

export function Sidebar({
  collapsed,
  pendingApprovals = 0,
}: {
  collapsed: boolean;
  pendingApprovals?: number;
}) {
  const raw = usePathname() ?? "/";
  // Normalize the trailing slash (next.config sets trailingSlash: true) so exact
  // matches against the hrefs in nav.ts work.
  const pathname = raw !== "/" && raw.endsWith("/") ? raw.slice(0, -1) : raw;
  const regulus = useRegulus();

  const [connectOpen, setConnectOpen] = useState(false);
  // Read localStorage-derived connection info after mount so the static prerender
  // and first client render agree (no hydration mismatch).
  const [conn, setConn] = useState<{ host: string }>({
    host: "127.0.0.1:8000",
  });
  useEffect(() => {
    setConn({ host: getApiBase() || "127.0.0.1:8000" });
  }, []);

  return (
    <div className={`console-sidebar-frame${collapsed ? " is-collapsed" : ""}`}>
      <aside
        aria-label="Primary navigation"
        aria-hidden={collapsed}
        className="console-sidebar"
        style={{
          height: "100%",
          background: "var(--bg-chrome)",
          borderRight: "1px solid var(--hair)",
          display: "flex",
          flexDirection: "column",
        }}
      >
      {/* Brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "13px 16px 16px" }}>
          <img
            aria-hidden="true"
            className="console-sidebar-brand-mark"
            src="/console/zeroth-mark.png"
            width={28}
            height={28}
            alt=""
          />
          <span
            style={{
              fontSize: 14,
              fontWeight: 600,
              color: "var(--text-primary)",
              letterSpacing: "-0.01em",
            }}
          >
            Zeroth
          </span>
        <span
          className="console-sidebar-version"
          style={{
            marginLeft: "auto",
            fontSize: 10.5,
            color: "var(--text-faint)",
          }}
        >
          v{VERSION}
        </span>
      </div>

      {/* Nav groups */}
      <nav
        className="console-sidebar-nav"
        style={{ flex: 1, overflowY: "auto", padding: "0 8px 8px", display: "flex", flexDirection: "column", gap: 18 }}
      >
        {NAV.map((group) => {
          if (group.gated === "regulus" && regulus !== "enabled") return null;
          return (
            <div key={group.heading}>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 500,
                  color: "var(--text-faint)",
                  padding: "0 8px 5px",
                }}
              >
                {group.heading}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
              {group.items.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`console-sidebar-link${active ? " is-active" : ""}`}
                    aria-current={active ? "page" : undefined}
                  >
                    <span className="console-sidebar-link-label">
                      {item.label}
                    </span>
                    {item.badge === "approvals" && pendingApprovals > 0 && (
                      <span
                        style={{
                          fontVariantNumeric: "tabular-nums",
                          fontSize: 10.5,
                          fontWeight: 600,
                            color: "var(--warning)",
                        }}
                      >
                        {pendingApprovals}
                      </span>
                    )}
                  </Link>
                );
              })}
              </div>
            </div>
          );
        })}
      </nav>

      {/* Connection footer */}
      <button
        type="button"
        onClick={() => setConnectOpen(true)}
        title="Connection settings"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          textAlign: "left",
          padding: "12px 16px",
          background: "transparent",
          border: "none",
          borderTop: "1px solid var(--hair)",
          cursor: "pointer",
        }}
      >
        <StatusDot tone="success" pulse />
        <span style={{ minWidth: 0, flex: 1 }}>
          <span
            style={{
              display: "block",
              fontSize: 12,
              color: "var(--text-muted)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {conn.host}
          </span>
        </span>
      </button>

        {connectOpen && <ConnectBar onClose={() => setConnectOpen(false)} />}
      </aside>

    </div>
  );
}
