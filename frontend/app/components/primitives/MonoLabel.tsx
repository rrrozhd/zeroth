// UI primitive: a mono-uppercase section label. Thin styled wrapper.

type MonoLabelProps = React.HTMLAttributes<HTMLSpanElement>;

export function MonoLabel({ style, children, ...rest }: MonoLabelProps) {
  return (
    <span
      style={{
        fontFamily: "var(--font-sans)",
        fontSize: 11,
        fontWeight: 500,
        color: "var(--text-muted)",
        ...style,
      }}
      {...rest}
    >
      {children}
    </span>
  );
}
