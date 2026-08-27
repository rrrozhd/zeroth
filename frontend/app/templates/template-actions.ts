import { ApiError, deleteTemplateVersion, type IdentityResponse } from "@/app/lib/api";

export type TemplateMutationAccess = {
  allowed: boolean;
  scope: string | null;
  roles: string;
  explanation: string;
};

const READ_ONLY_BUILTIN_ROLES = new Set(["operator", "reviewer"]);

/**
 * Identity exposes role names, not the configured role registry. Built-in roles
 * are deterministic; unknown configured roles must be decided by the API so the
 * client does not incorrectly deny a legitimate template:admin grant.
 */
export function templateMutationAccess(
  identity: IdentityResponse | null,
  identityError: string | null,
  identityLoading = false,
): TemplateMutationAccess {
  if (identityLoading && !identity) {
    return {
      allowed: false,
      scope: null,
      roles: "verifying",
      explanation: "Verifying role, capability, and scope before showing create/delete controls.",
    };
  }
  if (identityError || !identity) {
    return {
      allowed: false,
      scope: null,
      roles: "unavailable",
      explanation:
        "Role and scope could not be verified; create/delete hidden until identity is available.",
    };
  }

  const roles = identity.roles.length > 0 ? identity.roles.join(" · ") : "unavailable";
  const scope = `${identity.tenant_id} / ${identity.workspace_id ?? "tenant-wide"}`;
  if (identity.roles.some((role) => role === "admin" || role === "platform_admin")) {
    return {
      allowed: true,
      scope,
      roles,
      explanation: "Create and delete are enabled by template:admin for this scope.",
    };
  }
  if (
    identity.roles.length === 0 ||
    identity.roles.every((role) => READ_ONLY_BUILTIN_ROLES.has(role))
  ) {
    return {
      allowed: false,
      scope,
      roles,
      explanation: `${roles} does not include template:admin; templates are read-only in this scope.`,
    };
  }
  return {
    allowed: true,
    scope,
    roles,
    explanation: "This configured role can request changes; the API will validate template:admin.",
  };
}

export function templateDeleteConflictMessage(
  error: unknown,
  name: string,
  version: number,
): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  return (
    `Delete blocked: ${name}@v${version} is still in use. ` +
    `Remove or repin its dependent references, then try again. ${error.message}`
  );
}

export async function deleteConfirmedTemplateVersion(
  name: string,
  version: number,
  confirm: (message: string) => boolean = window.confirm,
  remove: (name: string, version: string) => Promise<unknown> = deleteTemplateVersion,
): Promise<boolean> {
  if (!confirm(`Delete ${name}@v${version}? This cannot be undone.`)) return false;
  await remove(name, String(version));
  return true;
}
