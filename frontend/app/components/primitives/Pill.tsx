// UI primitive: a small factual status/lifecycle/env badge. State is carried
// by the label and text color; the surface stays neutral across tones.

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
        background: "var(--bg-raised)",
        border: "1px solid transparent",
        borderRadius: 6,
        padding: "2px 8px",
        fontFamily: "var(--font-sans)",
        fontSize: 11.5,
        fontWeight: 500,
        lineHeight: 1.4,
        whiteSpace: "nowrap",
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
