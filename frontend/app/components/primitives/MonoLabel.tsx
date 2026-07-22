// UI primitive: a mono-uppercase section label. Thin styled wrapper.

type MonoLabelProps = React.HTMLAttributes<HTMLSpanElement>;

export function MonoLabel({ style, children, ...rest }: MonoLabelProps) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-muted)",
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
