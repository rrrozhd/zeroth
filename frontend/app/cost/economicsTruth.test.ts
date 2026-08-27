import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { NAV } from "@/app/components/nav";
import { fmtUsd } from "@/app/components/ui";
import { budgetFailureModeCopy, reconcileEconomics } from "./economicsTruth";

describe("economics presentation truth", () => {
  it("formats exact cents without false ten-thousandth precision", () => {
    expect(fmtUsd(0.01)).toBe("$0.01");
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(0.0012)).toBe("$0.0012");
    expect(fmtUsd(0.00000028)).toBe("$0.00000028");
  });

  it("keeps operation-level deployment spend separate from run attribution", () => {
    const result = reconcileEconomics(0.00001312, 0);

    expect(result).toEqual({
      ledgerActualUsd: 0.00001312,
      runAttributedUsd: 0,
      differenceUsd: 0.00001312,
      withinTolerance: false,
      explanation:
        "The difference is real deployment spend outside workflow-run attribution, such as provider verification or connector probes, or spend outside the latest run window.",
    });
  });

  it("uses the campaign acceptance tolerance for reconciled values", () => {
    expect(reconcileEconomics(0.001, 0.0010005).withinTolerance).toBe(true);
    expect(reconcileEconomics(1, 0.9949).withinTolerance).toBe(false);
  });

  it("describes the effective budget failure mode instead of static fail-open guidance", () => {
    expect(budgetFailureModeCopy("fail_closed")).toBe(
      "Fail-closed: new provider spend is denied when Regulus cannot authorize it.",
    );
    expect(budgetFailureModeCopy("fail_open")).toBe(
      "Fail-open: provider spend may proceed when Regulus is unavailable.",
    );
    expect(budgetFailureModeCopy(null)).toBe(
      "Failure mode unavailable until effective economics configuration loads.",
    );
  });

  it("uses one Economics navigation workspace instead of separate Cost and Regulus groups", () => {
    const headings = NAV.map((group) => group.heading);
    const items = NAV.flatMap((group) => group.items);

    expect(headings).not.toContain("Regulus");
    expect(items.find((item) => item.href === "/cost")?.label).toBe("Economics");
    expect(items.filter((item) => item.href.startsWith("/regulus")).map((item) => item.label))
      .toEqual(["Capabilities", "Enforcement"]);
  });

  it("does not present a design default or failed fetch as observed cost data", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/cost/page.tsx"), "utf8");

    expect(source).not.toContain('PER_RUN_CEILING_DEFAULT = "$2.00"');
    expect(source).toContain("getEconomicsConfiguration");
    expect(source).toContain("Fetch failed");
    expect(source).toContain("Not measured");
    expect(source).toContain('load.error.startsWith("403")');
  });

  it("keeps a real tenth-percent ratio visible instead of rounding it to zero", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/cost/page.tsx"), "utf8");

    expect(source).toContain('if (Math.abs(pct) < 0.1) return pct > 0 ? "<0.1%"');
    expect(source).toContain('if (Math.abs(pct) < 10) return `${pct.toFixed(1)');
  });

  it("explains restricted economics sections instead of presenting them as outages", () => {
    for (const file of ["app/regulus/costing/page.tsx", "app/regulus/reconciliation/page.tsx"]) {
      const source = fs.readFileSync(path.join(process.cwd(), file), "utf8");
      expect(source).toContain('message.startsWith("403")');
      expect(source).toContain('title={restricted ? "Access restricted"');
    }
  });

  it("uses adaptive precision and distinguishes Regulus valuation from the spend ledger", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/regulus/page.tsx"), "utf8");

    expect(source).toContain('import { fmtUsd } from "@/app/components/ui"');
    expect(source).not.toContain("function fmtUsd(n: number)");
    expect(source).toContain("Valuation model, not the spend ledger");
    expect(source).toContain("No synthetic or measured outcomes");
    expect(source).toContain("have been valued in this campaign yet");
    expect(source).toContain("Valued execution cost");
    expect(source).toContain("Recorded outcome value");
    expect(source).toContain("No valued capabilities yet");
    expect(source).toContain("Registry records remain available under Capabilities");
  });

  it("keeps adaptive micro-cost precision across Regulus detail surfaces", () => {
    const capabilities = fs.readFileSync(
      path.join(process.cwd(), "app/regulus/capabilities/page.tsx"),
      "utf8",
    );
    const costing = fs.readFileSync(
      path.join(process.cwd(), "app/regulus/costing/page.tsx"),
      "utf8",
    );

    expect(capabilities).toContain('import { fmtUsd } from "@/app/components/ui"');
    expect(capabilities).not.toContain("maximumFractionDigits: 2");
    expect(costing).toContain('fmtUsd as fmtKnownUsd');
    expect(costing).toContain("return fmtKnownUsd(n)");
    expect(costing).not.toContain("abs.toFixed(4)");

    const rightsizing = fs.readFileSync(
      path.join(process.cwd(), "app/rightsizing/page.tsx"),
      "utf8",
    );
    expect(rightsizing).toContain('import { fmtUsd } from "@/app/components/ui"');
    expect(rightsizing).not.toContain("function fmtUsd(n: number)");
  });

  it("uses adaptive currency precision in deployment inspection", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/deployments/page.tsx"), "utf8");

    expect(source).toContain("fmtUsd(cost.data.total_cost_usd)");
    expect(source).not.toContain("n.toFixed(4)");
  });

  it("labels deployment spend with its actual production ledger source", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/cost/page.tsx"), "utf8");

    expect(source).toContain("Actual provider spend");
    expect(source).toContain("Actual spend by deployment");
    expect(source).toContain("Source: production cost ledger");
    expect(source).not.toContain("Source: Regulus deployment KPI");
  });

  it("separates control proofs and open exposure from actual provider spend", () => {
    const source = fs.readFileSync(path.join(process.cwd(), "app/cost/page.tsx"), "utf8");

    expect(source).toContain("Control proofs excluded");
    expect(source).toContain("synthetic_control_usd");
    expect(source).toContain("active_exposure_usd");
    expect(source).toContain("ambiguous_exposure_usd");
    expect(source).toContain('import { fmtUsd } from "@/app/components/ui"');
    expect(source).toContain("reconcileEconomics");
    expect(source).toContain("Run-attributed economics");
    expect(source).toContain("No priced workflow runs in this window");
  });

  it("keeps budget-exposure geometry scoped to the Economics surface", () => {
    const pageSource = fs.readFileSync(path.join(process.cwd(), "app/cost/page.tsx"), "utf8");
    const moduleSource = fs.readFileSync(path.join(process.cwd(), "app/cost/cost.module.css"), "utf8");
    const globalSource = fs.readFileSync(path.join(process.cwd(), "app/globals.css"), "utf8");

    expect(pageSource).toContain("styles.controlBand");
    expect(moduleSource).toContain(".controlBand");
    expect(globalSource).not.toContain(".cost-control-grid");
  });
});
