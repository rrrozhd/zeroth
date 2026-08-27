import type { IdentityResponse } from "@/app/lib/api";

export type RegulusAccess = {
  canRead: boolean;
  canMutate: boolean;
  scope: string | null;
  roles: string;
  source: "builtin" | "api" | "unavailable";
};

const BUILTIN_ROLES = new Set(["operator", "reviewer", "admin", "platform_admin"]);

/** Resolve deterministic built-in access without pretending the client knows
 * configured role grants. Unknown roles remain API-authoritative. */
export function regulusAccess(
  identity: IdentityResponse | null,
  identityError: string | null,
): RegulusAccess {
  if (identityError || !identity) {
    return {
      canRead: false,
      canMutate: false,
      scope: null,
      roles: "unavailable",
      source: "unavailable",
    };
  }

  const roles = identity.roles.length > 0 ? identity.roles.join(" · ") : "unavailable";
  const scope = `${identity.tenant_id} / ${identity.workspace_id ?? "tenant-wide"}`;
  if (identity.roles.some((role) => !BUILTIN_ROLES.has(role))) {
    return { canRead: true, canMutate: true, scope, roles, source: "api" };
  }

  return {
    canRead: identity.roles.some((role) => role === "admin" || role === "platform_admin"),
    canMutate: identity.roles.includes("platform_admin"),
    scope,
    roles,
    source: "builtin",
  };
}
