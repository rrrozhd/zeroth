import {
  ConsoleSection,
  ConsoleSurface,
} from "@/app/components/primitives";

import styles from "./economics.module.css";

function classes(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

export function EconomicsPanel({
  title,
  children,
  density = "default",
  className,
  surfaceClassName,
  evidenceScope,
}: {
  title?: React.ReactNode;
  children: React.ReactNode;
  density?: "default" | "compact" | "flush";
  className?: string;
  surfaceClassName?: string;
  evidenceScope?: string;
}) {
  const surface = (
    <ConsoleSurface density={density} className={surfaceClassName} evidenceScope={evidenceScope}>
      {children}
    </ConsoleSurface>
  );

  if (title == null) return surface;

  return (
    <ConsoleSection
      title={title}
      className={classes(styles.panel, className)}
      evidenceScope={evidenceScope}
    >
      {surface}
    </ConsoleSection>
  );
}
