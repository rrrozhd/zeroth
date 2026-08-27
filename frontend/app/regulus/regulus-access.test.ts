import { describe, expect, it } from "vitest";

import { regulusAccess } from "./regulus-access";

const identity = (roles: string[], tenant = "tenant-a", workspace: string | null = "workspace-a") => ({
  subject: `${roles.join("-") || "unknown"}-subject`,
  roles,
  tenant_id: tenant,
  workspace_id: workspace,
});

describe("Regulus governance access", () => {
  it.each(["operator", "reviewer"])(
    "fails closed before protected reads for the built-in %s role",
    (role) => {
      expect(regulusAccess(identity([role]), null)).toMatchObject({
        canRead: false,
        canMutate: false,
        scope: "tenant-a / workspace-a",
        roles: role,
        source: "builtin",
      });
    },
  );

  it("allows tenant administrators to inspect but not change global enforcement", () => {
    expect(regulusAccess(identity(["admin"], "tenant-b", null), null)).toMatchObject({
      canRead: true,
      canMutate: false,
      scope: "tenant-b / tenant-wide",
      roles: "admin",
      source: "builtin",
    });
  });

  it("allows platform administrators to inspect and decide enforcement actions", () => {
    expect(regulusAccess(identity(["platform_admin"]), null)).toMatchObject({
      canRead: true,
      canMutate: true,
      source: "builtin",
    });
  });

  it("defers configured-role permission decisions to the stable API", () => {
    expect(regulusAccess(identity(["governance_reviewer"]), null)).toMatchObject({
      canRead: true,
      canMutate: true,
      source: "api",
    });
  });

  it("fails closed when identity cannot be established", () => {
    expect(regulusAccess(null, "401 unauthenticated")).toMatchObject({
      canRead: false,
      canMutate: false,
      scope: null,
      roles: "unavailable",
      source: "unavailable",
    });
  });
});
