import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const primaryBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const twinBase = process.env.ZEROTH_EVALUATION_TWIN_API_BASE ?? "http://127.0.0.1:8123";
const secretRoot = process.env.ZEROTH_EVALUATION_ROLE_SECRET_ROOT
  ?? "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets";

const fixtures = [
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "operator", file: "tenant-a-operator-key", retention: false },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "reviewer", file: "tenant-a-reviewer-key", retention: false },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "admin", file: "tenant-a-admin-key", retention: true },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "platform_admin", file: "service-api-key", retention: true },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "operator", file: "tenant-b-operator-key", retention: false },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "reviewer", file: "tenant-b-reviewer-key", retention: false },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "admin", file: "tenant-b-admin-key", retention: true },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "platform_admin", file: "tenant-b-platform-admin-key", retention: true },
] as const;

function credential(filename: string): string {
  return readFileSync(path.join(secretRoot, filename), "utf8").trim();
}

for (const fixture of fixtures) {
  test(`${fixture.tenant} ${fixture.role} has an inspectable authoritative scope`, async ({ page, request }, testInfo) => {
    test.skip(!liveEnabled, "requires the isolated local evaluation services");
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    coverCriteria(
      testInfo,
      "identity.authoritative-scope",
      "identity.tenant-isolation",
      "identity.retention-tenant-isolation",
      `identity.role.${fixture.role}`,
    );
    const key = credential(fixture.file);

    await configurePage(page, fixture.service, fixture.tenant, key);
    await page.goto("/console/", { waitUntil: "networkidle" });
    const scope = page.locator(".console-topbar-breadcrumb");
    await expect(scope).toHaveAttribute(
      "aria-label",
      `Scope: ${fixture.tenant} / tenant-wide; roles: ${fixture.role}`,
    );
    await expect(page.locator(".console-topbar-role")).toHaveText(fixture.role);

    const wrongTenant = await request.get(`${fixture.other}/v1/identity`, {
      headers: { "X-API-Key": key },
    });
    expect(wrongTenant.status()).toBe(404);
    const ownPolicy = await request.get(`${fixture.service}/v1/retention/policy`, {
      headers: { "X-API-Key": key },
    });
    expect(ownPolicy.status()).toBe(fixture.retention ? 200 : 403);
    const wrongPolicy = await request.get(`${fixture.other}/v1/retention/policy`, {
      headers: { "X-API-Key": key },
    });
    expect(wrongPolicy.status()).toBe(fixture.retention ? 404 : 403);
    const wrongPolicyBody = await wrongPolicy.json() as Record<string, unknown>;
    expect(Object.keys(wrongPolicyBody)).toEqual(["detail"]);
    const wrongHolds = await request.get(`${fixture.other}/v1/retention/legal-holds`, {
      headers: { "X-API-Key": key },
    });
    expect(wrongHolds.status()).toBe(fixture.retention ? 404 : 403);
    const wrongHoldsBody = await wrongHolds.json() as Record<string, unknown>;
    expect(Object.keys(wrongHoldsBody)).toEqual(["detail"]);
    await attachSafeJson(testInfo, "scope-result", {
      tenant_id: fixture.tenant,
      role: fixture.role,
      own_service_status: 200,
      cross_tenant_status: wrongTenant.status(),
      own_retention_policy_status: ownPolicy.status(),
      cross_tenant_retention_policy_status: wrongPolicy.status(),
      cross_tenant_legal_holds_status: wrongHolds.status(),
      cross_tenant_retention_payload_fields: Object.keys(wrongPolicyBody),
      cross_tenant_legal_holds_payload_fields: Object.keys(wrongHoldsBody),
    });
    await testInfo.attach(`scope-${fixture.tenant}-${fixture.role}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });
}
