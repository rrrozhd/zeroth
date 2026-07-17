"use client";

// UI primitive: a compact action button. Variants: primary (teal tint),
// neutral (hairline border), danger (red tint). Radius 6, mono 12px, dims when
// disabled. Hover brightens the tint. All `<button>`/aria props pass through.

import { forwardRef, useState } from "react";

type Variant = "primary" | "neutral" | "danger";

const VARIANT: Record<Variant, { base: React.CSSProperties; hoverBg: string }> = {
  primary: {
    base: {
      background: "rgba(94,234,212,0.12)",
      color: "var(--accent)",
      border: "1px solid transparent",
    },
    hoverBg: "rgba(94,234,212,0.20)",
  },
  neutral: {
    base: {
      background: "transparent",
      color: "var(--text-secondary)",
      border: "1px solid var(--hair-strong)",
    },
    hoverBg: "var(--bg-raised)",
  },
  danger: {
    base: {
      background: "rgba(248,113,113,0.12)",
      color: "var(--danger)",
      border: "1px solid transparent",
    },
    hoverBg: "rgba(248,113,113,0.20)",
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
        fontFamily: "var(--font-mono)",
        fontSize: 12,
        fontWeight: 500,
        lineHeight: 1,
        padding: "7px 12px",
        borderRadius: 6,
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
