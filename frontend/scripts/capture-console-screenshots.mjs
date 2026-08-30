#!/usr/bin/env node
/** Capture the real authenticated console surfaces used by README and docs. */

import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { chromium } from "@playwright/test";

function option(name, fallback) {
  const prefix = `--${name}=`;
  return process.argv.find((value) => value.startsWith(prefix))?.slice(prefix.length) ?? fallback;
}

const apiKey = process.env.ZEROTH_SCREENSHOT_API_KEY;
if (!apiKey) throw new Error("ZEROTH_SCREENSHOT_API_KEY is required");

const baseUrl = option("base-url", "http://127.0.0.1:3000");
const apiBase = option("api-base", "http://127.0.0.1:8122");
const tenant = option("tenant", "evaluation-studio-v1");
const workflow = option("workflow", "pilot-mcp-tool-demo");
const output = resolve(option("output", "../docs/assets/console"));
const surfaces = [
  ["overview", "/console/"],
  ["studio", "/console/studio/"],
  ["mcp-tool-workflow", `/console/studio/edit/?id=${encodeURIComponent(workflow)}`],
  ["audit", "/console/audit/"],
  ["economics", "/console/cost/"],
  ["rightsizing", "/console/rightsizing/"],
  ["retention", "/console/retention/"],
  ["artifacts", "/console/artifacts/"],
];

await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1360, height: 860 } });
  const session = await context.request.post(`${apiBase.replace(/\/+$/, "")}/v1/auth/session`, {
    headers: { "X-API-Key": apiKey },
  });
  if (session.status() !== 204) throw new Error(`browser session exchange returned ${session.status()}`);
  await context.addInitScript(({ base, scope }) => {
    localStorage.setItem("zeroth.apiBase", base);
    localStorage.removeItem("zeroth.apiKey");
    localStorage.setItem("zeroth.sessionActive", "1");
    localStorage.setItem("zeroth.env", "pilot-local");
    localStorage.setItem("zeroth.tenant", scope);
  }, { base: apiBase, scope: tenant });
  const page = await context.newPage();
  for (const [name, path] of surfaces) {
    const response = await page.goto(`${baseUrl}${path}`, { waitUntil: "networkidle" });
    if (!response?.ok()) throw new Error(`${path} returned ${response?.status() ?? "no response"}`);
    await page.locator("main").waitFor({ state: "visible" });
    await page.waitForTimeout(400);
    await page.screenshot({ path: resolve(output, `${name}.png`), animations: "disabled" });
  }
} finally {
  await browser.close();
}

console.log(`Captured ${surfaces.length} authenticated console screenshots.`);
