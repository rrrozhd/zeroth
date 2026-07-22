"use client";

// UI primitive: a monospaced code block on bg-code with a copy button.

import { useState } from "react";

type CodeBlockProps = {
  /** The code/text to render and copy. */
  code: string;
  /** Optional mono-uppercase label above the block. */
  label?: React.ReactNode;
  style?: React.CSSProperties;
};

export function CodeBlock({ code, label, style }: CodeBlockProps) {
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
            fontFamily: "var(--font-mono)",
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-muted)",
            marginBottom: 8,
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
          top: label != null ? 30 : 8,
          right: 8,
          fontFamily: "var(--font-mono)",
          fontSize: 10.5,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          color: copied ? "var(--success)" : "var(--text-muted)",
          background: "var(--bg-raised)",
          border: "1px solid var(--hair)",
          borderRadius: 5,
          padding: "3px 7px",
          cursor: "pointer",
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <pre
        style={{
          margin: 0,
          background: "var(--bg-code)",
          color: "var(--text-code)",
          border: "1px solid var(--hair)",
          borderRadius: 8,
          fontFamily: "var(--font-mono)",
          fontSize: 12.5,
          lineHeight: 1.75,
          overflowX: "auto",
          padding: "12px 14px",
        }}
      >
        {code}
      </pre>
    </div>
  );
}
