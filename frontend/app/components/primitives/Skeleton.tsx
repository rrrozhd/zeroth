// UI primitive: a pulsing placeholder block for loading states.
// Uses the `z-pulse` keyframe from globals.css.

type SkeletonProps = Omit<React.HTMLAttributes<HTMLDivElement>, "className"> & {
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  className?: string;
};

export function Skeleton({
  width = "100%",
  height = 14,
  radius = 6,
  className = "",
  style,
  ...rest
}: SkeletonProps) {
  return (
    <div
      aria-hidden
      className={`z-pulse ${className}`.trim()}
      style={{
        width,
        height,
        borderRadius: radius,
        background: "var(--bg-raised)",
        ...style,
      }}
      {...rest}
    />
  );
}
