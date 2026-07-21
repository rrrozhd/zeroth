// UI primitive: a small bordered status/lifecycle/env badge.
// tone -> text color + 12% tint bg + 30% tint border. Mono uppercase 10.5px.

import { TONE, type Tone } from "./StatusDot";

type PillProps = React.HTMLAttributes<HTMLSpanElement> & {
  tone?: Tone | string;
};

export function Pill({ tone = "neutral", style, children, ...rest }: PillProps) {
  const c = TONE[tone as string] ?? tone;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        color: c,
        background: `color-mix(in srgb, ${c} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${c} 30%, transparent)`,
        borderRadius: 5,
        padding: "2px 7px",
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        fontWeight: 500,
        lineHeight: 1.4,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        whiteSpace: "nowrap",
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
