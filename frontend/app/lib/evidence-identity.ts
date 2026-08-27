const INTERACTIVE_SELECTOR = [
  "button",
  "a[href]",
  "input:not([type='hidden'])",
  "select",
  "textarea",
  "summary",
  "[contenteditable='true']",
  "[role='button']",
  "[role='checkbox']",
  "[role='combobox']",
  "[role='radio']",
  "[role='slider']",
  "[role='switch']",
  "[role='textbox']",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export type EvidenceIdentityResult = {
  controls: HTMLElement[];
  errors: string[];
};

function shortHash(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(36).padStart(7, "0");
}

function slug(value: string): string {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized.length <= 72
    ? normalized
    : `${normalized.slice(0, 64)}-${shortHash(normalized)}`;
}

const generatedIdentities = new WeakMap<HTMLElement, string>();

function routeName(pathname: string): string {
  const parts = pathname.split("/").filter(Boolean);
  const consoleIndex = parts.indexOf("console");
  const surface = parts[consoleIndex + 1] ?? parts[0] ?? "overview";
  return slug(surface) || "overview";
}

function labelledByText(control: HTMLElement): string {
  const ids = control.getAttribute("aria-labelledby")?.split(/\s+/).filter(Boolean) ?? [];
  return ids
    .map((id) => control.ownerDocument.getElementById(id)?.textContent?.trim() ?? "")
    .filter(Boolean)
    .join(" ");
}

function nativeLabelText(control: HTMLElement): string {
  if (!(control instanceof HTMLInputElement || control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement)) {
    return "";
  }
  return Array.from(control.labels ?? [])
    .map((label) => label.textContent?.trim() ?? "")
    .filter(Boolean)
    .join(" ");
}

function accessibleEvidenceName(control: HTMLElement): string {
  return (
    control.getAttribute("aria-label") ||
    labelledByText(control) ||
    nativeLabelText(control) ||
    control.getAttribute("name") ||
    control.getAttribute("placeholder") ||
    control.getAttribute("title") ||
    control.textContent?.trim() ||
    (control instanceof HTMLAnchorElement ? control.getAttribute("href") : "") ||
    ""
  );
}

function controlKind(control: HTMLElement): string {
  const role = control.getAttribute("role");
  if (role && role !== "button") return slug(role);
  if (control instanceof HTMLAnchorElement) return "link";
  return slug(control.tagName) || "control";
}

function evidenceScope(control: HTMLElement): string {
  const scopes: string[] = [];
  let current = control.parentElement?.closest<HTMLElement>("[data-evidence-scope]") ?? null;
  while (current) {
    const scope = slug(current.dataset.evidenceScope ?? "");
    if (scope) scopes.unshift(scope);
    current = current.parentElement?.closest<HTMLElement>("[data-evidence-scope]") ?? null;
  }
  return scopes.join(".");
}

export function evidenceIdentityOf(control: Element): string | null {
  return control.getAttribute("data-evidence-id");
}

export function assignEvidenceIdentities(
  root: ParentNode,
  pathname: string,
): EvidenceIdentityResult {
  const controls = Array.from(root.querySelectorAll<HTMLElement>(INTERACTIVE_SELECTOR));
  const errors: string[] = [];
  const seen = new Set<string>();
  const surface = routeName(pathname);

  for (const control of controls) {
    const existing = evidenceIdentityOf(control);
    const generated = generatedIdentities.get(control);
    const explicit = existing && existing !== generated ? existing : null;
    const name = explicit ? "" : slug(accessibleEvidenceName(control));
    if (!explicit && !name) {
      errors.push(
        `interactive control has no accessible evidence name: ${control.tagName.toLowerCase()}`,
      );
      continue;
    }
    const scope = explicit ? "" : evidenceScope(control);
    const identity = explicit ?? [surface, scope, controlKind(control), name].filter(Boolean).join(".");
    if (control.getAttribute("data-evidence-id") !== identity) {
      control.setAttribute("data-evidence-id", identity);
    }
    if (explicit) generatedIdentities.delete(control);
    else generatedIdentities.set(control, identity);
    if (seen.has(identity)) errors.push(`duplicate evidence identity: ${identity}`);
    seen.add(identity);
  }

  return { controls, errors };
}
