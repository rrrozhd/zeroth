import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function source(path: string): string {
  const url = new URL(path, import.meta.url);
  return existsSync(url) ? readFileSync(url, "utf8") : "";
}

describe("console layout and accessibility regressions", () => {
  it("gives Audit readiness and records explicit parent-owned spacing", () => {
    const page = source("./audit/page.tsx");
    const css = source("./audit/audit.module.css");

    expect(page).toContain("className={styles.contentStack}");
    expect(css).toMatch(/\.contentStack\s*\{[^}]*display:\s*grid;[^}]*gap:\s*var\(--space-4\)/);
  });

  it("distinguishes configured audit signing from session verification", () => {
    const page = source("./audit/page.tsx");

    expect(page).toContain('"Signing configured"');
    expect(page).toContain('"Chain not verified this session"');
    expect(page).not.toContain('"Signed audit ready"');
  });

  it("announces each Overview checklist item's completion state", () => {
    const page = source("./page.tsx");

    expect(page).toContain('role="list"');
    expect(page).toContain('role="listitem"');
    expect(page).toContain('it.done ? "completed" : "not completed"');
  });

  it("uses container-aware Overview deployment rows that can reflow", () => {
    const page = source("./page.tsx");
    const css = source("./globals.css");

    expect(page).toContain('className="overview-page"');
    expect(page).toContain('className="overview-deployment-row"');
    expect(css).toContain("container: overview-page / inline-size");
    expect(css).toMatch(/@container overview-page[^}]+\.overview-checklist-list/);
    expect(css).toMatch(/\.overview-deployment-row\s*\{[^}]*flex-wrap:\s*wrap/);
  });

  it("places Getting started below health as a horizontal strip before full-width operations", () => {
    const page = source("./page.tsx");
    const css = source("./globals.css");

    expect(page).toMatch(
      /<ChecklistCard items=\{checklist\} \/>[\s\S]*?<div className="overview-primary-column">/,
    );
    expect(page).toContain('className="overview-checklist-list"');
    expect(css).toMatch(
      /\.overview-checklist-list\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/,
    );
    expect(css).not.toContain("grid-template-columns: minmax(0, 1.6fr) minmax(220px, 1fr)");
  });

  it("exposes the rightsizing comparison as a radiogroup and explains disabled submission", () => {
    const page = source("./rightsizing/page.tsx");

    expect(page).toContain('role="radiogroup"');
    expect(page).toContain('role="radio"');
    expect(page).toContain("aria-checked={mode === m}");
    expect(page).toContain('"experiment-disabled-reason"');
    expect(page).toContain('aria-describedby={!canSubmit ? "experiment-disabled-reason"');
  });

  it("does not animate the whole hydrated Rightsizing page in WebKit", () => {
    const page = source("./rightsizing/page.tsx");

    expect(page).not.toContain('<div className="z-fade"');
  });

  it("does not attach the transform fade to any hydrated page root in WebKit", () => {
    const pageRoots = [
      "./page.tsx",
      "./approvals/page.tsx",
      "./connectors/page.tsx",
      "./guide/page.tsx",
      "./metrics/page.tsx",
      "./retention/page.tsx",
      "./webhooks/page.tsx",
    ];

    for (const path of pageRoots) {
      expect(source(path), path).not.toMatch(/<div[^>]+className="[^"]*z-fade[^"]*"/);
    }
  });

  it("keeps the current sidebar route visually selected while another link is hovered", () => {
    const css = source("./globals.css");

    expect(css).not.toContain(
      ".console-sidebar-nav:hover .console-sidebar-link.is-active:not(:hover)",
    );
    expect(css).not.toContain(
      ".console-sidebar-link.is-active:not(:focus-visible)",
    );
  });

  it("keeps persisted quality coverage visible when model spend is zero", () => {
    const page = source("./rightsizing/page.tsx");

    expect(page).toContain("function QualityCoveragePanel");
    expect(page).toContain("<QualityCoveragePanel quality={report.quality} />");
  });

  it("lays out experiment fields and actions with responsive named grids", () => {
    const page = source("./rightsizing/page.tsx");
    const css = source("./rightsizing/rightsizing.module.css");

    expect(page).toContain("styles.experimentIdentityGrid");
    expect(page).toContain("styles.experimentOptionsGrid");
    expect(page).toContain("styles.experimentActionFooter");
    expect(css).toMatch(/@media \(max-width: 42rem\)/);
  });

  it("uses the interface font for prose checkbox labels", () => {
    const page = source("./rightsizing/page.tsx");
    const checkbox = page.match(/function Checkbox\([\s\S]*?\n}\n/)?.[0] ?? "";

    expect(checkbox).not.toContain("fontFamily: MONO");
    expect(checkbox).toContain('fontFamily: "var(--font-sans)"');
  });

  it("renders a redacted error as its safe status instead of a wall of markers", () => {
    const page = source("./audit/page.tsx");

    expect(page).toContain('const REDACTED_ERROR = "***REDACTED***"');
    expect(page).toContain("r.error !== REDACTED_ERROR");
    expect(page).not.toContain('parts.push("· redacted")');
  });

  it("shows the verifier reason instead of truncating a failed audit ID", () => {
    const page = source("./audit/page.tsx");

    expect(page).toContain('`chain broken · ${result.error}`');
    expect(page).not.toContain("result.failed_audit_id.slice(0, 8)");
  });
});
