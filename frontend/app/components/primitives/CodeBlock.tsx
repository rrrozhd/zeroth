"use client";

// UI primitive: compact code/payload block with a copy action.

import { useState } from "react";

type CodeBlockProps = {
  /** The code/text to render and copy. */
  code: string;
  /** Optional label above the block. */
  label?: React.ReactNode;
  /** Unique accessible name when the same visible label repeats on a page. */
  ariaLabel?: string;
  style?: React.CSSProperties;
};

export function CodeBlock({ code, label, ariaLabel, style }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div style={{ position: "relative", ...style }}>
      {label != null && (
        <div
          style={{
            fontFamily: "var(--font-sans)",
            fontSize: 11.5,
            fontWeight: 500,
            color: "var(--text-muted)",
            marginBottom: 6,
          }}
        >
          {label}
        </div>
      )}
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy code"}
        style={{
          position: "absolute",
          top: label != null ? 26 : 7,
          right: 7,
          fontFamily: "var(--font-sans)",
          fontSize: 11,
          color: copied ? "var(--success)" : "var(--text-muted)",
          background: "var(--bg-card)",
          border: "1px solid var(--hair-strong)",
          borderRadius: 6,
          padding: "3px 7px",
          cursor: "pointer",
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <pre
        role="region"
        aria-label={ariaLabel ?? (typeof label === "string" ? `${label} code` : "Code content")}
        tabIndex={0}
        style={{
          margin: 0,
          background: "var(--bg-code)",
          color: "var(--text-code)",
          border: "1px solid var(--hair)",
          borderRadius: 8,
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          lineHeight: 1.6,
          overflowX: "auto",
          padding: "11px 56px 11px 13px",
        }}
      >
        {code}
      </pre>
    </div>
  );
}
