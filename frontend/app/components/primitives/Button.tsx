"use client";

// UI primitive matching the approved Console v2 compact controls.

import { forwardRef, useState } from "react";

type Variant = "primary" | "neutral" | "danger";

const VARIANT: Record<Variant, { base: React.CSSProperties; hoverBg: string }> = {
  primary: {
    base: {
      background: "var(--accent)",
      color: "var(--accent-ink)",
      border: "1px solid var(--accent)",
    },
    hoverBg: "#413e7c",
  },
  neutral: {
    base: {
      background: "var(--bg-card)",
      color: "var(--text-secondary)",
      border: "1px solid var(--hair-strong)",
    },
    hoverBg: "var(--bg-raised)",
  },
  danger: {
    base: {
      background: "transparent",
      color: "var(--danger)",
      border: "1px solid var(--danger)",
    },
    hoverBg: "rgba(169,47,40,0.10)",
  },
};

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "neutral", disabled, style, onMouseEnter, onMouseLeave, children, ...rest },
  ref,
) {
  const [hover, setHover] = useState(false);
  const v = VARIANT[variant];
  return (
    <button
      ref={ref}
      disabled={disabled}
      onMouseEnter={(e) => {
        setHover(true);
        onMouseEnter?.(e);
      }}
      onMouseLeave={(e) => {
        setHover(false);
        onMouseLeave?.(e);
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 6,
        fontFamily: "var(--font-sans)",
        fontSize: 12.5,
        fontWeight: 500,
        lineHeight: 1,
        minHeight: "var(--control-height)",
        whiteSpace: "nowrap",
        flexShrink: 0,
        padding: "6px 12px",
        borderRadius: 8,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.45 : 1,
        transition: "background 120ms ease, opacity 120ms ease",
        ...v.base,
        ...(hover && !disabled ? { background: v.hoverBg } : null),
        ...style,
      }}
      {...rest}
    >
      {children}
    </button>
  );
});
