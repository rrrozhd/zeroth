import { forwardRef } from "react";

import styles from "./ConsoleLayout.module.css";

function classes(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

type CommonProps = {
  children: React.ReactNode;
  className?: string;
};

export function ConsolePage({ children, className }: CommonProps) {
  // Safari can retain the completed transform animation as an empty compositor
  // layer for large, hydrated tables: the accessibility tree remains complete
  // while the entire content pane is visually blank. Page boundaries therefore
  // stay unanimated; local controls and overlays can still animate safely.
  return <div className={classes(styles.page, className)}>{children}</div>;
}

export function ConsolePageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <header className={classes(styles.pageHeader, className)}>
      <div>
        <h1 className={styles.pageTitle}>{title}</h1>
        {description != null && <p className={styles.pageDescription}>{description}</p>}
      </div>
      {actions != null && <div className={styles.pageActions}>{actions}</div>}
    </header>
  );
}

export function ConsoleSection({
  title,
  meta,
  actions,
  children,
  className,
  evidenceScope,
}: CommonProps & {
  title: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  evidenceScope?: string;
}) {
  return (
    <section className={classes(styles.section, className)} data-evidence-scope={evidenceScope}>
      <header className={styles.sectionHeader}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        {actions ?? (meta != null ? <span className={styles.sectionMeta}>{meta}</span> : null)}
      </header>
      {children}
    </section>
  );
}

export function ConsoleSurface({
  children,
  className,
  density = "default",
  evidenceScope,
}: CommonProps & { density?: "default" | "compact" | "flush"; evidenceScope?: string }) {
  return (
    <div
      data-evidence-scope={evidenceScope}
      className={classes(
        styles.surface,
        density === "compact" && styles.surfaceCompact,
        density === "flush" && styles.surfaceFlush,
        className,
      )}
    >
      {children}
    </div>
  );
}

export function ConsoleDataList({
  children,
  ariaLabel,
  className,
}: CommonProps & { ariaLabel: string }) {
  return (
    <div role="list" aria-label={ariaLabel} className={classes(styles.dataList, className)}>
      {children}
    </div>
  );
}

export function ConsoleDataRow({ children, className }: CommonProps) {
  return (
    <div role="listitem" className={classes(styles.dataRow, className)}>
      {children}
    </div>
  );
}

export function ConsoleMetricBand({
  children,
  columns = 3,
  ariaLabel = "Metrics",
  className,
}: CommonProps & {
  columns?: 2 | 3 | 4 | 5;
  ariaLabel?: string;
}) {
  return (
    <div
      role="list"
      aria-label={ariaLabel}
      data-columns={columns}
      className={classes(styles.metricBand, className)}
    >
      {children}
    </div>
  );
}

export function ConsoleMetric({
  label,
  value,
  detail,
  tone = "default",
  className,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  detail?: React.ReactNode;
  tone?: "default" | "danger" | "success";
  className?: string;
}) {
  return (
    <div role="listitem" className={classes(styles.metric, className)}>
      <span className={styles.metricLabel}>{label}</span>
      <strong
        className={classes(
          styles.metricValue,
          tone === "danger" && styles.metricValueDanger,
          tone === "success" && styles.metricValueSuccess,
        )}
      >
        {value}
      </strong>
      {detail != null && <span className={styles.metricDetail}>{detail}</span>}
    </div>
  );
}

export function ConsoleTableFrame({
  children,
  className,
  ariaLabel,
}: CommonProps & { ariaLabel: string }) {
  return (
    <div className={classes(styles.tableFrame, className)}>
      <div
        className={styles.tableScroll}
        role="region"
        aria-label={ariaLabel}
        tabIndex={0}
      >
        {children}
      </div>
    </div>
  );
}

export function ConsoleField({
  label,
  hint,
  required = false,
  children,
  className,
}: CommonProps & {
  label: React.ReactNode;
  hint?: React.ReactNode;
  required?: boolean;
}) {
  return (
    <label className={classes(styles.field, className)}>
      <span className={styles.fieldLabel}>
        {label}
        {required && (
          <span className={styles.fieldRequired} aria-hidden="true">
            *
          </span>
        )}
      </span>
      {children}
      {hint != null && <span className={styles.fieldHint}>{hint}</span>}
    </label>
  );
}

export const consoleControlClassName = styles.control;

export const ConsoleInput = forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(function ConsoleInput({ className, ...props }, ref) {
  return <input ref={ref} className={classes(styles.control, className)} {...props} />;
});

export function ConsoleNotice({
  title,
  tone = "neutral",
  actions,
  children,
  className,
}: CommonProps & {
  title?: React.ReactNode;
  tone?: "neutral" | "danger" | "success";
  actions?: React.ReactNode;
}) {
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={classes(
        styles.notice,
        title == null && styles.noticeUntitled,
        actions != null && styles.noticeWithActions,
        tone === "danger" && styles.noticeDanger,
        tone === "success" && styles.noticeSuccess,
        className,
      )}
    >
      {title != null && <strong className={styles.noticeTitle}>{title}</strong>}
      <span>{children}</span>
      {actions != null && <div className={styles.noticeActions}>{actions}</div>}
    </div>
  );
}

export function ConsoleEmpty({ children, className }: CommonProps) {
  return <p className={classes(styles.empty, className)}>{children}</p>;
}

export function ConsoleMeta({
  children,
  className,
  ...rest
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span className={classes(styles.meta, className)} {...rest}>
      {children}
    </span>
  );
}
