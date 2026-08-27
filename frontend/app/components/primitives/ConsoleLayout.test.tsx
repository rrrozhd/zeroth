import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { Button } from "./Button";

import {
  ConsoleDataList,
  ConsoleDataRow,
  ConsoleField,
  ConsoleMetric,
  ConsoleMetricBand,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleTableFrame,
  consoleControlClassName,
} from "./ConsoleLayout";

describe("console layout primitives", () => {
  it("owns one consistent vertical rhythm for every direct page section", () => {
    const css = readFileSync(new URL("./ConsoleLayout.module.css", import.meta.url), "utf8");

    expect(css).toMatch(/\.page\s*\{[^}]*display:\s*grid;[^}]*gap:\s*var\(--space-6\)/);
    expect(css).not.toMatch(/\.pageHeader\s*\{[^}]*margin-bottom:/);
  });

  it("does not animate the page compositor boundary in Safari", () => {
    const html = renderToStaticMarkup(<ConsolePage>Audit records</ConsolePage>);

    expect(html).not.toContain("z-fade");
  });

  it("leaves vertical section rhythm to the parent layout", () => {
    const css = readFileSync(new URL("./ConsoleLayout.module.css", import.meta.url), "utf8");

    expect(css).not.toMatch(/\.section\s*\+\s*\.section/);
  });

  it("keeps default buttons the same height as console inputs", () => {
    const html = renderToStaticMarkup(<Button>Save</Button>);

    expect(html).toContain("min-height:var(--control-height)");
  });

  it("renders an operational page hierarchy without card scaffolding", () => {
    const html = renderToStaticMarkup(
      <ConsolePage>
        <ConsolePageHeader
          title="Studio"
          description="Compose and inspect governed workflows."
          actions={<button type="button">Refresh</button>}
        />
        <ConsoleSection title="Workflows" meta="2 records">
          <ConsoleDataList ariaLabel="Workflows">
            <ConsoleDataRow>First workflow</ConsoleDataRow>
            <ConsoleDataRow>Second workflow</ConsoleDataRow>
          </ConsoleDataList>
        </ConsoleSection>
      </ConsolePage>,
    );

    expect(html).toContain("<h1");
    expect(html).toContain("Studio");
    expect(html).toContain('role="list"');
    expect(html).toContain('role="listitem"');
    expect(html).toContain("2 records");
    expect(html).not.toContain("CARD");
  });

  it("keeps labels, required state, controls, and recovery copy explicit", () => {
    const html = renderToStaticMarkup(
      <>
        <ConsoleField label="Workflow name" hint="Used in run history." required>
          <input className={consoleControlClassName} required />
        </ConsoleField>
        <ConsoleNotice tone="danger" title="Could not save" actions={<button>Retry</button>}>
          Check the API connection and retry.
        </ConsoleNotice>
      </>,
    );

    expect(html).toContain("Workflow name");
    expect(html).toContain("Used in run history.");
    expect(html).toContain("required");
    expect(html).toContain('role="alert"');
    expect(html).toContain("Check the API connection and retry.");
    expect(html).toContain("Retry");
  });

  it("renders measured metrics and wide data as semantic operational regions", () => {
    const html = renderToStaticMarkup(
      <>
        <ConsoleMetricBand columns={2} ariaLabel="Economics totals">
          <ConsoleMetric label="AI spend" value="$14.27" detail="Measured this month" />
          <ConsoleMetric label="Net margin" value="$3.08" tone="success" />
        </ConsoleMetricBand>
        <ConsoleTableFrame ariaLabel="Audit records">
          <table aria-label="Audit records"><tbody><tr><td>record</td></tr></tbody></table>
        </ConsoleTableFrame>
      </>,
    );

    expect(html).toContain('aria-label="Economics totals"');
    expect(html.match(/role="listitem"/g)).toHaveLength(2);
    expect(html).toContain("Measured this month");
    expect(html).toContain('aria-label="Audit records"');
  });
});
