import { readFileSync } from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

type Catalog = {
  capabilities: Array<{
    capability_id: string;
    routes: string[];
    control_patterns: string[];
  }>;
};

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const inventoryWorkflowId = process.env.ZEROTH_EVALUATION_INVENTORY_WORKFLOW_ID
  ?? "da5da69b-1086-4cfe-8090-424a0118b88c";
const catalog = JSON.parse(
  readFileSync(path.resolve(process.cwd(), "../release/product_validation/catalog-v1.json"), "utf8"),
) as Catalog;
const routes = [...new Set(catalog.capabilities.flatMap((capability) => capability.routes))].sort();
const interactiveSelector = "button,a[href],input:not([type=hidden]),select,textarea,summary,[contenteditable=true],[role=button],[role=checkbox],[role=combobox],[role=radio],[role=slider],[role=switch],[role=textbox],[tabindex]:not([tabindex='-1'])";
const productInteractiveSelector = interactiveSelector
  .split(",")
  .map((selector) => `.console-shell ${selector}`)
  .join(",");

function matchesPattern(value: string, pattern: string): boolean {
  const expression = pattern
    .split("*")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${expression}$`).test(value);
}

function patternSource(pattern: string): string {
  return pattern
    .split("*")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
}

test.describe("published product control inventory", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test.beforeEach(async ({ page }) => {
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  for (const route of routes) {
    test(`${route} exposes named evidence identities for every interactive control`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop-1440", "functional inventory is captured once");
      coverCriteria(testInfo, `controls${route === "/" ? ".overview" : route.replaceAll("/", ".")}`);
      const target = route === "/"
        ? "/"
        : route === "/studio/edit"
          ? `/studio/edit/?id=${encodeURIComponent(inventoryWorkflowId)}`
          : `${route}/`;
      await page.goto(`/console${target}`, { waitUntil: "domcontentloaded" });
      await expect(page.locator("main")).toBeVisible();
      const routeSpecificPatterns = catalog.capabilities
        .filter((capability) => (
          capability.capability_id !== "shell-navigation" && capability.routes.includes(route)
        ))
        .flatMap((capability) => capability.control_patterns)
        .map(patternSource);
      expect(routeSpecificPatterns.length, `catalog has no route-specific controls for ${route}`).toBeGreaterThan(0);
      await expect.poll(async () => page.locator(productInteractiveSelector).evaluateAll(
        (controls, sources) => controls.filter((control) => {
          const element = control as HTMLElement;
          const style = window.getComputedStyle(element);
          const visible = element.getClientRects().length > 0
            && style.display !== "none"
            && style.visibility !== "hidden";
          const identity = element.dataset.evidenceId;
          return visible && identity != null && sources.some((source) => (
            new RegExp(`^${source}$`).test(identity)
          ));
        }).length,
        routeSpecificPatterns,
      ), {
        message: `${route} did not reach a route-specific interactive state`,
        timeout: 20_000,
      }).toBeGreaterThan(0);
      await expect.poll(async () => page.evaluate((selector) => {
        const controls = Array.from(document.querySelectorAll<HTMLElement>(selector)).filter((control) => {
          const style = window.getComputedStyle(control);
          return control.getClientRects().length > 0
            && style.display !== "none"
            && style.visibility !== "hidden";
        });
        return {
          unnamed: controls.filter((control) => !control.dataset.evidenceId).length,
          identity_errors: JSON.parse(
            document.documentElement.dataset.evidenceIdentityErrors ?? "[]",
          ) as string[],
        };
      }, productInteractiveSelector), {
        message: `${route} evidence identities did not settle`,
        timeout: 20_000,
      }).toEqual({ unnamed: 0, identity_errors: [] });

      const inventory = await page.evaluate((selector) => {
        const controls = Array.from(document.querySelectorAll<HTMLElement>(selector)).filter((control) => {
          const style = window.getComputedStyle(control);
          return control.getClientRects().length > 0
            && style.display !== "none"
            && style.visibility !== "hidden";
        });
        return {
          controls: controls.map((control) => ({
            evidence_id: control.dataset.evidenceId ?? null,
            tag: control.tagName.toLowerCase(),
            type: control.getAttribute("type"),
            role: control.getAttribute("role"),
            name: control.getAttribute("role") === "radio"
              ? control.closest('[role="radiogroup"]')?.getAttribute("aria-label")
                ?? control.closest('[role="radiogroup"]')?.getAttribute("data-evidence-id")
                ?? null
              : control.getAttribute("name"),
            value: control instanceof HTMLInputElement && control.type === "radio"
              ? control.value
              : control.getAttribute("role") === "radio"
                ? control.getAttribute("value")
                  ?? control.getAttribute("data-value")
                  ?? control.getAttribute("data-mode")
                  ?? control.dataset.evidenceId
                  ?? control.textContent?.trim()
                  ?? ""
                : control.getAttribute("value"),
            required: control.hasAttribute("required") || control.getAttribute("aria-required") === "true",
            disabled: control.matches(":disabled") || control.getAttribute("aria-disabled") === "true",
            minimum: control.getAttribute("min"),
            maximum: control.getAttribute("max"),
            minimum_length: control.getAttribute("minlength"),
            maximum_length: control.getAttribute("maxlength"),
            pattern: control.getAttribute("pattern"),
            options: control instanceof HTMLSelectElement
              ? Array.from(control.options).map((option) => ({
                  value: option.value,
                  disabled: option.disabled,
                }))
              : [],
          })),
          identity_errors: JSON.parse(document.documentElement.dataset.evidenceIdentityErrors ?? "[]") as string[],
        };
      }, productInteractiveSelector);
      expect(inventory.controls.length).toBeGreaterThan(0);
      expect(inventory.controls.every((control) => control.evidence_id)).toBe(true);
      expect(inventory.identity_errors).toEqual([]);
      const routeCapabilities = catalog.capabilities.filter((capability) => capability.routes.includes(route));
      const cataloged = inventory.controls.map((control) => ({
        ...control,
        capability_ids: routeCapabilities
          .filter((capability) => capability.control_patterns.some((pattern) => (
            control.evidence_id != null && matchesPattern(control.evidence_id, pattern)
          )))
          .map((capability) => capability.capability_id),
      }));
      expect(
        cataloged.filter((control) => control.capability_ids.length === 0),
        "visible control is absent from the validation catalog",
      ).toEqual([]);

      const exercisedOptions: Array<{ evidence_id: string | null; values: string[] }> = [];
      for (const select of await page.locator("select:visible:not(:disabled)").all()) {
        const initial = await select.inputValue();
        const evidenceId = await select.getAttribute("data-evidence-id");
        const values = await select.locator("option:not(:disabled)").evaluateAll((options) => (
          options.map((option) => (option as HTMLOptionElement).value)
        ));
        for (const value of values) {
          await select.selectOption(value);
          await expect(select).toHaveValue(value);
        }
        await select.selectOption(initial);
        exercisedOptions.push({ evidence_id: evidenceId, values });
      }

      const exercisedBinaryControls: Array<{
        evidence_id: string | null;
        states: boolean[];
        appeared: string[];
        disappeared: string[];
      }> = [];
      for (const control of await page.locator('input[type="checkbox"]:visible:not(:disabled),[role="checkbox"]:visible:not([aria-disabled="true"]),[role="switch"]:visible:not([aria-disabled="true"])').all()) {
        const native = await control.evaluate((element) => (
          element instanceof HTMLInputElement && element.type === "checkbox"
        ));
        const readState = async () => native
          ? control.isChecked()
          : (await control.getAttribute("aria-checked")) === "true";
        const initial = await readState();
        const before = new Set(await page.locator("[data-evidence-id]").evaluateAll((elements) => (
          elements.map((element) => (element as HTMLElement).dataset.evidenceId!).filter(Boolean)
        )));
        if (native) {
          await control.setChecked(!initial);
          await expect(control).toBeChecked({ checked: !initial });
        } else {
          await control.click();
          await expect.poll(readState).toBe(!initial);
        }
        const after = new Set(await page.locator("[data-evidence-id]").evaluateAll((elements) => (
          elements.map((element) => (element as HTMLElement).dataset.evidenceId!).filter(Boolean)
        )));
        if (native) {
          await control.setChecked(initial);
        } else {
          await control.click();
          await expect.poll(readState).toBe(initial);
        }
        exercisedBinaryControls.push({
          evidence_id: await control.getAttribute("data-evidence-id"),
          states: [initial, !initial],
          appeared: [...after].filter((identity) => !before.has(identity)).sort(),
          disappeared: [...before].filter((identity) => !after.has(identity)).sort(),
        });
      }

      const exercisedRadioOptions: Array<{
        evidence_id: string | null;
        name: string | null;
        value: string;
      }> = [];
      const radioGroups = new Map<string, string | null>();
      for (const radio of await page.locator('input[type="radio"]:visible:not(:disabled),[role="radio"]:visible:not([aria-disabled="true"])').all()) {
        const evidenceId = await radio.getAttribute("data-evidence-id");
        const native = await radio.evaluate((element) => (
          element instanceof HTMLInputElement && element.type === "radio"
        ));
        const name = native
          ? await radio.getAttribute("name")
          : await radio.evaluate((element) => (
            element.closest('[role="radiogroup"]')?.getAttribute("aria-label")
              ?? element.closest('[role="radiogroup"]')?.getAttribute("data-evidence-id")
              ?? null
          ));
        const value = native
          ? await radio.getAttribute("value") ?? "on"
          : await radio.evaluate((element) => (
            element.getAttribute("value")
              ?? element.getAttribute("data-value")
              ?? element.getAttribute("data-mode")
              ?? (element as HTMLElement).dataset.evidenceId
              ?? element.textContent?.trim()
              ?? ""
          ));
        const group = name ?? evidenceId ?? "";
        if (!radioGroups.has(group)) {
          const checked = native
            ? await page.locator(`input[type="radio"][name=${JSON.stringify(name ?? "")}]`).evaluateAll(
              (items) => items.find((item) => (item as HTMLInputElement).checked)?.getAttribute("data-evidence-id") ?? null,
            )
            : await radio.evaluate((element) => (
              element.closest('[role="radiogroup"]')
                ?.querySelector('[role="radio"][aria-checked="true"]')
                ?.getAttribute("data-evidence-id") ?? null
            ));
          radioGroups.set(group, checked);
        }
        if (native) {
          await radio.check();
          await expect(radio).toBeChecked();
        } else {
          await radio.click();
          await expect(radio).toHaveAttribute("aria-checked", "true");
        }
        exercisedRadioOptions.push({ evidence_id: evidenceId, name, value });
      }
      for (const [name, initialEvidenceId] of radioGroups) {
        if (initialEvidenceId != null) {
          const initial = page.locator(`[data-evidence-id=${JSON.stringify(initialEvidenceId)}]`);
          if (await initial.getAttribute("role") === "radio") {
            await initial.click();
            await expect(initial).toHaveAttribute("aria-checked", "true");
          } else {
            await initial.check();
          }
        } else if (name !== "") {
          // A radio group with no initial selection cannot be restored to empty via ordinary UI.
          // The interaction is retained in this isolated campaign form and never submitted.
        }
      }

      await attachSafeJson(testInfo, `control-inventory-${route.replaceAll("/", "-") || "overview"}`, {
        controls: cataloged,
        identity_errors: inventory.identity_errors,
        exercised_select_options: exercisedOptions,
        exercised_checkbox_states: exercisedBinaryControls,
        exercised_radio_options: exercisedRadioOptions,
      });
      await testInfo.attach(`checkpoint-${route.replaceAll("/", "-") || "overview"}`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
    });
  }
});
