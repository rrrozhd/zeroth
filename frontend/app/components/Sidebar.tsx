"use client";

// The console's left navigation rail. 212px, dark chrome, right hairline.
// Brand block at the top, grouped nav in the middle (the Regulus group appears
// only when the econ plane is actually mounted), and a connection footer that
// opens the Connect bar.

import { useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV } from "./nav";
import { VERSION } from "@/app/lib/version";
import { getApiBase, getApiKey } from "@/app/lib/config";
import { useRegulus } from "./regulusContext";
import { ConnectBar } from "./ConnectBar";
import { StatusDot } from "./primitives";

export function Sidebar({ pendingApprovals = 0 }: { pendingApprovals?: number }) {
  const raw = usePathname() ?? "/";
  // Normalize the trailing slash (next.config sets trailingSlash: true) so exact
  // matches against the hrefs in nav.ts work.
  const pathname = raw !== "/" && raw.endsWith("/") ? raw.slice(0, -1) : raw;
  const regulus = useRegulus();

  const [connectOpen, setConnectOpen] = useState(false);
  // Read localStorage-derived connection info after mount so the static prerender
  // and first client render agree (no hydration mismatch).
  const [conn, setConn] = useState<{ host: string; key: string }>({
    host: "127.0.0.1:8000",
    key: "",
  });
  useEffect(() => {
    setConn({ host: getApiBase() || "127.0.0.1:8000", key: getApiKey() });
  }, []);

  return (
    <aside
      style={{
        width: 212,
        minWidth: 212,
        height: "100%",
        background: "var(--bg-chrome)",
        borderRight: "1px solid var(--hair)",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Brand */}
      <div style={{ padding: "16px 16px 12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* next/image so the src is prefixed with the configured basePath
              (/console); a plain <img> would not be and would 404. */}
          <Image
            src="/zeroth-mark.svg"
            alt=""
            width={14}
            height={14}
            style={{ display: "block", borderRadius: 3 }}
          />
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 15,
              fontWeight: 600,
              color: "var(--text-primary)",
            }}
          >
            zeroth<span style={{ color: "var(--text-faint)" }}>/core</span>
          </span>
        </div>
        <div
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 10.5,
            color: "var(--text-faint)",
            marginTop: 6,
            paddingLeft: 22,
          }}
        >
          v{VERSION} · console
        </div>
      </div>

      {/* Nav groups */}
      <nav style={{ flex: 1, overflowY: "auto", padding: "4px 10px 12px" }}>
        {NAV.map((group) => {
          if (group.gated === "regulus" && regulus !== "enabled") return null;
          return (
            <div key={group.heading} style={{ marginBottom: 14 }}>
              <div
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 10,
                  fontWeight: 600,
                  textTransform: "uppercase",
                  letterSpacing: "0.12em",
                  color: "var(--text-faint)",
                  padding: "4px 8px",
                }}
              >
                {group.heading}
              </div>
              {group.items.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 9,
                      padding: "6px 8px",
                      borderRadius: 6,
                      textDecoration: "none",
                      background: active ? "rgba(94,234,212,0.09)" : "transparent",
                      color: active ? "var(--accent)" : "var(--text-secondary)",
                    }}
                  >
                    <span
                      aria-hidden
                      style={{
                        display: "inline-block",
                        width: 5,
                        height: 5,
                        borderRadius: 1,
                        background: active ? "var(--accent)" : "var(--text-faint)",
                      }}
                    />
                    <span style={{ fontSize: 13, flex: 1 }}>{item.label}</span>
                    {item.badge === "approvals" && pendingApprovals > 0 && (
                      <span
                        style={{
                          fontFamily: "var(--font-mono)",
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
          padding: "10px 14px",
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
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              color: "var(--text-secondary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {conn.host}
          </span>
          <span
            style={{
              display: "block",
              fontFamily: "var(--font-mono)",
              fontSize: 10.5,
              color: "var(--text-faint)",
            }}
          >
            {conn.key.slice(0, 13)}••••
          </span>
        </span>
      </button>

      {connectOpen && <ConnectBar onClose={() => setConnectOpen(false)} />}
    </aside>
  );
}
