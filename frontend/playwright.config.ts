import { defineConfig, devices } from "@playwright/test";

const evidenceRoot = process.env.ZEROTH_EVALUATION_BROWSER_ROOT ?? "output/playwright";
const withoutServer = process.env.PLAYWRIGHT_NO_SERVER === "1";
const baseURL = process.env.ZEROTH_EVALUATION_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 30_000,
  outputDir: `${evidenceRoot}/artifacts`,
  reporter: [
    ["html", { outputFolder: `${evidenceRoot}/html-report`, open: "never" }],
    ["./e2e/support/evidence-reporter.ts"],
    ["list"],
  ],
  use: {
    baseURL,
    screenshot: "only-on-failure",
    trace: "off",
    video: "on",
  },
  webServer: withoutServer
    ? undefined
    : {
        command: "npm run dev",
        // Next is mounted under /console; probing / would remain a legitimate
        // 404 forever and make Playwright misdiagnose a ready server as hung.
        url: `${baseURL}/console/`,
        reuseExistingServer: false,
        timeout: 120_000,
      },
  projects: [
    { name: "desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "webkit-1440", use: { ...devices["Desktop Safari"], viewport: { width: 1440, height: 900 } } },
    { name: "desktop-1280", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "tablet-768", use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } } },
    { name: "mobile-390", use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } } },
  ],
});
