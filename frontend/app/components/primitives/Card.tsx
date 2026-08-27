// UI primitive matching the approved Console v2 white rounded surfaces.
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
        border: "1px solid #e4e3ed",
        borderRadius: 12,
        padding: pad,
        ...style,
      }}
      {...rest}
    >
      {label != null && (
        <div
          style={{
            fontFamily: "var(--font-sans)",
            fontSize: 11.5,
            fontWeight: 500,
            color: "var(--text-muted)",
            marginBottom: 8,
          }}
        >
          {label}
        </div>
      )}
      {children}
    </section>
  );
}
