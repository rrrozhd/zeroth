// UI primitive: a surface card. bg-card on a hairline border, radius 8.
// Optional mono-uppercase `label` header slot. Thin styled wrapper — no logic.

type CardProps = React.HTMLAttributes<HTMLElement> & {
  /** Optional mono-uppercase header label. */
  label?: React.ReactNode;
  /** Inner padding in px (handoff uses 14–16). */
  pad?: number;
};

export function Card({ label, pad = 16, style, children, ...rest }: CardProps) {
  return (
    <section
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--hair)",
        borderRadius: 8,
        padding: pad,
        ...style,
      }}
      {...rest}
    >
      {label != null && (
        <div
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-muted)",
            marginBottom: 10,
          }}
        >
          {label}
        </div>
      )}
      {children}
    </section>
  );
}
