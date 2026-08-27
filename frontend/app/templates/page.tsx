"use client";

// The Templates screen — a master-detail view over the prompt-template registry.
// Left rail: every registered template version (listTemplates → the `.templates`
// envelope array). Right: the selected template's detail (name@vN, its declared
// variables as chips, and the Jinja2 body), or the inline "new template" form.
//
// listTemplates() returns a TemplateListResponse ({ templates: TemplateResponse[] })
// and every row is already a FULL TemplateResponse (name, version, template_str,
// variables, description) — so the detail panel renders straight from the selected
// row. getTemplate(name) is intentionally unused: it takes only a name (can't
// target a specific version) and would refetch data the list already carries.
//
// Every mutation (create, delete-version) toasts and refetches. The API key lives
// only in localStorage (lib/config) — never logged, never placed in a URL. Nothing
// here crashes when the API is unconfigured or unreachable: useLoad turns failures
// into an inline error state.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Card,
  CodeBlock,
  MonoLabel,
  Pill,
  Skeleton,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  ApiError,
  createTemplate,
  errMsg,
  getIdentity,
  listTemplates,
  type Template,
  type TemplateList,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";
import {
  deleteConfirmedTemplateVersion,
  templateDeleteConflictMessage,
  templateMutationAccess,
  type TemplateMutationAccess,
} from "./template-actions";

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/** A stable per-row key: a template name can carry several versions. */
function keyOf(t: Template): string {
  return `${t.name}@${t.version}`;
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function TemplatesPage() {
  const templates = useLoad<TemplateList>(listTemplates);
  const identity = useLoad(getIdentity);
  const mutationAccess = templateMutationAccess(identity.data, identity.error, identity.loading);

  // localStorage-derived config is read after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = templates.data?.templates ?? [];
  const selected = useMemo(
    () => list.find((t) => keyOf(t) === selectedKey) ?? null,
    [list, selectedKey],
  );

  function select(t: Template) {
    setCreating(false);
    setSelectedKey(keyOf(t));
  }

  function openCreate() {
    setCreating(true);
    setSelectedKey(null);
  }

  // After a create, refetch and select the freshly-registered template.
  function onCreated(t: Template) {
    setCreating(false);
    setSelectedKey(keyOf(t));
    templates.reload();
  }

  // After a delete, the selected version is gone — clear it and refetch.
  function onDeleted() {
    setSelectedKey(null);
    templates.reload();
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <h1 className="sr-only">Templates</h1>
      <ListPane
        templates={templates}
        connected={connected}
        mounted={mounted}
        mutationAccess={mutationAccess}
        selectedKey={selectedKey}
        onSelect={select}
        onNew={openCreate}
      />
      <div
        role="region"
        aria-label="Template details"
        data-evidence-id="templates.region.details"
        tabIndex={0}
        style={{ flex: 1, minWidth: 0, overflowY: "auto" }}
      >
        {creating ? (
          <CreateForm onCreated={onCreated} onCancel={() => setCreating(false)} />
        ) : selected ? (
          <TemplateDetail
            key={selectedKey ?? ""}
            template={selected}
            mutationAccess={mutationAccess}
            onDeleted={onDeleted}
          />
        ) : (
          <DetailPlaceholder />
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Left list (280px)
// --------------------------------------------------------------------------

function ListPane({
  templates,
  connected,
  mounted,
  mutationAccess,
  selectedKey,
  onSelect,
  onNew,
}: {
  templates: Loadable<TemplateList>;
  connected: boolean;
  mounted: boolean;
  mutationAccess: TemplateMutationAccess;
  selectedKey: string | null;
  onSelect: (t: Template) => void;
  onNew: () => void;
}) {
  const list = templates.data?.templates ?? [];
  return (
    <aside
      aria-label="Template list"
      style={{
        width: 280,
        flexShrink: 0,
        borderRight: "1px solid var(--hair)",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        background: "var(--bg-chrome)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          padding: "13px 14px",
          borderBottom: "1px solid var(--hair)",
        }}
      >
        <MonoLabel>Templates</MonoLabel>
        {mutationAccess.allowed && (
          <Button
            data-evidence-id="templates-new"
            variant="primary"
            onClick={onNew}
            style={{ padding: "4px 9px" }}
          >
            + New
          </Button>
        )}
      </div>

      <div
        data-evidence-id="templates-scope"
        style={{
          padding: "9px 14px",
          borderBottom: "1px solid var(--hair)",
          color: "var(--text-faint)",
          fontSize: 10.5,
          lineHeight: 1.45,
        }}
      >
        {mutationAccess.scope && (
          <span style={{ display: "block", color: "var(--text-muted)" }}>
            {mutationAccess.scope} · {mutationAccess.roles}
          </span>
        )}
        {mutationAccess.explanation}
      </div>

      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        {templates.loading && !templates.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 14 }}>
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} height={34} />
            ))}
          </div>
        ) : templates.error ? (
          <div style={{ padding: 14 }}>
            <InlineError message={templates.error} onRetry={templates.reload} />
          </div>
        ) : mounted && !connected ? (
          <EmptyNote>Connect to the API (top bar) to load templates.</EmptyNote>
        ) : list.length === 0 ? (
          <EmptyNote>
            No templates yet — create one with <b>+ New</b>.
          </EmptyNote>
        ) : (
          list.map((t) => (
            <TemplateRow
              key={keyOf(t)}
              template={t}
              selected={keyOf(t) === selectedKey}
              onSelect={() => onSelect(t)}
            />
          ))
        )}
      </div>
    </aside>
  );
}

function TemplateRow({
  template: t,
  selected,
  onSelect,
}: {
  template: Template;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      data-evidence-id={`templates-version-row.${encodeURIComponent(keyOf(t))}`}
      type="button"
      onClick={onSelect}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        width: "100%",
        textAlign: "left",
        cursor: "pointer",
        padding: "10px 14px",
        border: "none",
        borderLeft: `2px solid ${selected ? "var(--accent)" : "transparent"}`,
        borderBottom: "1px solid var(--hair)",
        background: selected ? "color-mix(in srgb, var(--accent) 7%, transparent)" : "transparent",
        color: "inherit",
        transition: "background 120ms ease",
      }}
    >
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          color: "var(--text-primary)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          flex: 1,
          minWidth: 0,
        }}
      >
        {t.name}
      </span>
      <Pill tone="accent" style={{ flexShrink: 0 }}>
        v{t.version}
      </Pill>
    </button>
  );
}

// --------------------------------------------------------------------------
// Detail — renders straight from the selected row (already a full
// TemplateResponse). name@vN header, delete-version, variable chips, Jinja2 body.
// --------------------------------------------------------------------------

function TemplateDetail({
  template: t,
  mutationAccess,
  onDeleted,
}: {
  template: Template;
  mutationAccess: TemplateMutationAccess;
  onDeleted: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [deleteConflict, setDeleteConflict] = useState<string | null>(null);
  const vars = t.variables;

  async function doDelete() {
    setDeleteConflict(null);
    setBusy(true);
    try {
      if (!(await deleteConfirmedTemplateVersion(t.name, t.version))) {
        setBusy(false);
        return;
      }
      toast(`Deleted ${t.name}@v${t.version}`);
      onDeleted();
    } catch (e) {
      const conflict = templateDeleteConflictMessage(e, t.name, t.version);
      if (conflict) setDeleteConflict(conflict);
      toast(conflict ?? `Delete failed: ${errMsg(e)}`);
      setBusy(false);
    }
  }

  return (
    <div
      data-evidence-id="templates-detail"
      style={{ padding: "22px 26px", display: "flex", flexDirection: "column", gap: 16 }}
    >
      {/* Header: name@vN + delete-version */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 17, fontWeight: 600 }}>
          {t.name}
          <span style={{ color: "var(--text-muted)" }}>@v{t.version}</span>
        </span>
        {mutationAccess.allowed && (
          <div style={{ marginLeft: "auto" }}>
            <Button
              data-evidence-id="templates-delete-version"
              variant="danger"
              disabled={busy}
              onClick={doDelete}
            >
              {busy ? "Deleting…" : "Delete version"}
            </Button>
          </div>
        )}
      </div>

      {deleteConflict && (
        <p
          role="alert"
          data-evidence-id="templates-delete-conflict"
          style={{
            margin: 0,
            color: "var(--danger)",
            fontSize: 12,
            lineHeight: 1.55,
          }}
        >
          {deleteConflict}
        </p>
      )}

      {t.description && (
        <p style={{ margin: 0, fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
          {t.description}
        </p>
      )}

      {/* Variables */}
      <div>
        <MonoLabel style={{ display: "block", marginBottom: 8 }}>Variables</MonoLabel>
        {vars.length === 0 ? (
          <EmptyInline>No variables — this template renders as-is.</EmptyInline>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {vars.map((v) => (
              <VariableChip key={v} name={v} />
            ))}
          </div>
        )}
      </div>

      {/* Jinja2 body */}
      <div data-evidence-id="templates-detail-body">
        <CodeBlock label="Template body (Jinja2)" code={t.template_str} />
      </div>

      <p style={{ margin: 0, fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
        Secrets are redacted in audit records.
      </p>
    </div>
  );
}

function VariableChip({ name }: { name: string }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: "var(--text-secondary)",
        background: "color-mix(in srgb, var(--agent) 12%, transparent)",
        border: "1px solid color-mix(in srgb, var(--agent) 30%, transparent)",
        borderRadius: 5,
        padding: "3px 8px",
        whiteSpace: "nowrap",
      }}
    >
      {`{{ ${name} }}`}
    </span>
  );
}

// --------------------------------------------------------------------------
// Create — the server owns Jinja2 parsing and variable extraction. The client
// deliberately omits `variables` so loops, filters, assignments, and nested
// expressions cannot be misdeclared by a partial browser-side parser.
// --------------------------------------------------------------------------

type CreateErrors = Partial<Record<"name" | "version" | "description" | "body", string>>;

const TEMPLATE_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

function CreateForm({
  onCreated,
  onCancel,
}: {
  onCreated: (t: Template) => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [version, setVersion] = useState("1");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<CreateErrors>({});
  const nameRef = useRef<HTMLInputElement>(null);
  const versionRef = useRef<HTMLInputElement>(null);
  const descriptionRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  function clearError(field: keyof CreateErrors) {
    setErrors((current) => {
      if (!current[field]) return current;
      const next = { ...current };
      delete next[field];
      return next;
    });
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const parsedVersion = Number(version.trim());
    const nextErrors: CreateErrors = {};
    const trimmedName = name.trim();
    if (!trimmedName) nextErrors.name = "Enter a template name.";
    else if (trimmedName.length > 128) nextErrors.name = "Use 128 characters or fewer.";
    else if (!TEMPLATE_NAME_PATTERN.test(trimmedName)) {
      nextErrors.name =
        "Use letters, numbers, dots, underscores, or hyphens, starting with a letter or number.";
    }
    if (!version.trim()) nextErrors.version = "Enter a version.";
    else if (!Number.isInteger(parsedVersion) || parsedVersion < 1 || parsedVersion > 1_000_000) {
      nextErrors.version = "Version must be a whole number from 1 to 1,000,000.";
    }
    if (description.length > 2_000) {
      nextErrors.description = "Use 2,000 characters or fewer.";
    }
    if (!body.trim()) nextErrors.body = "Enter a Jinja2 template body.";
    else if (body.length > 100_000) nextErrors.body = "Use 100,000 characters or fewer.";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      if (nextErrors.name) nameRef.current?.focus();
      else if (nextErrors.version) versionRef.current?.focus();
      else if (nextErrors.description) descriptionRef.current?.focus();
      else bodyRef.current?.focus();
      return;
    }

    setBusy(true);
    try {
      const created = await createTemplate({
        name: trimmedName,
        template_str: body,
        version: parsedVersion,
        description: description.trim(),
      });
      toast(`Created ${created.name}@v${created.version}`);
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setErrors((current) => ({
          ...current,
          body: "Template body is invalid Jinja2. Correct the syntax and try again.",
        }));
        bodyRef.current?.focus();
      } else if (err instanceof ApiError && err.status === 409) {
        setErrors((current) => ({
          ...current,
          name: "This template name and version already exists. Choose another version.",
        }));
        nameRef.current?.focus();
      }
      toast(`Create failed: ${errMsg(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ padding: "22px 26px", maxWidth: 660 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
        <span style={{ fontSize: 17, fontWeight: 600 }}>New template</span>
      </div>
      <p style={{ margin: "0 0 16px", fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
        A named, versioned Jinja2 prompt template. The server validates the body and extracts
        variables from the complete Jinja2 syntax tree.
      </p>

      <Card pad={16}>
        <form
          noValidate
          onSubmit={submit}
          style={{ display: "flex", flexDirection: "column", gap: 14 }}
        >
          <Field
            fieldId="templates-name"
            label="name"
            hint="Logical template name (e.g. grounded-answer)."
            required
            error={errors.name}
          >
            <TextInput
              inputRef={nameRef}
              evidenceId="templates-name"
              value={name}
              onChange={(value) => {
                setName(value);
                clearError("name");
              }}
              placeholder="my-template"
              maxLength={128}
              pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
              autoFocus
              required
              describedBy={`templates-name-hint${errors.name ? " templates-name-error" : ""}`}
              invalid={Boolean(errors.name)}
            />
          </Field>
          <Field
            fieldId="templates-version"
            label="version"
            hint="Whole number from 1 to 1,000,000."
            required
            error={errors.version}
          >
            <TextInput
              inputRef={versionRef}
              evidenceId="templates-version"
              value={version}
              onChange={(value) => {
                setVersion(value);
                clearError("version");
              }}
              placeholder="1"
              inputMode="numeric"
              required
              describedBy={`templates-version-hint${errors.version ? " templates-version-error" : ""}`}
              invalid={Boolean(errors.version)}
            />
          </Field>
          <Field
            fieldId="templates-description"
            label="description"
            hint="Optional — a short note shown in the detail view."
            error={errors.description}
          >
            <TextInput
              inputRef={descriptionRef}
              evidenceId="templates-description"
              value={description}
              onChange={(value) => {
                setDescription(value);
                clearError("description");
              }}
              placeholder="What this template is for"
              maxLength={2_000}
              describedBy={`templates-description-hint${errors.description ? " templates-description-error" : ""}`}
              invalid={Boolean(errors.description)}
            />
          </Field>
          <Field
            fieldId="templates-body"
            label="template_str"
            hint="The Jinja2 body."
            required
            error={errors.body}
          >
            <TextArea
              inputRef={bodyRef}
              evidenceId="templates-body"
              value={body}
              onChange={(value) => {
                setBody(value);
                clearError("body");
              }}
              placeholder={"Answer the question using only the context.\n\nQuestion: {{ question }}"}
              maxLength={100_000}
              required
              describedBy={`templates-body-hint${errors.body ? " templates-body-error" : ""}`}
              invalid={Boolean(errors.body)}
            />
          </Field>

          <div>
            <MonoLabel style={{ display: "block", marginBottom: 6 }}>
              Variable extraction
            </MonoLabel>
            <EmptyInline>
              The server extracts variables from the complete Jinja2 syntax tree when you create
              the template.
            </EmptyInline>
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <Button
              data-evidence-id="templates-create"
              type="submit"
              variant="primary"
              disabled={busy}
            >
              {busy ? "Creating…" : "Create template"}
            </Button>
            <Button
              data-evidence-id="templates-cancel"
              type="button"
              variant="neutral"
              onClick={onCancel}
              disabled={busy}
            >
              Cancel
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirrors the Deployments / Runs screen conventions)
// --------------------------------------------------------------------------

function Field({
  fieldId,
  label,
  hint,
  required,
  error,
  children,
}: {
  fieldId: string;
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ display: "block" }}>
      <label htmlFor={fieldId} style={{ display: "block" }}>
        <MonoLabel style={{ display: "block", marginBottom: 5 }}>
          {label}
          {required && <span aria-hidden="true"> *</span>}
        </MonoLabel>
      </label>
      {children}
      {hint && (
        <span
          id={`${fieldId}-hint`}
          style={{
            display: "block",
            marginTop: 5,
            fontSize: 11,
            color: "var(--text-faint)",
            lineHeight: 1.5,
          }}
        >
          {hint}
        </span>
      )}
      {error && (
        <span
          id={`${fieldId}-error`}
          role="alert"
          style={{
            display: "block",
            marginTop: 5,
            color: "var(--danger)",
            fontSize: 11,
            lineHeight: 1.5,
          }}
        >
          {error}
        </span>
      )}
    </div>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  autoFocus,
  inputMode,
  evidenceId,
  inputRef,
  required,
  describedBy,
  invalid = false,
  maxLength,
  pattern,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  inputMode?: "numeric";
  evidenceId?: string;
  inputRef?: React.RefObject<HTMLInputElement | null>;
  required?: boolean;
  describedBy?: string;
  invalid?: boolean;
  maxLength?: number;
  pattern?: string;
}) {
  return (
    <input
      ref={inputRef}
      id={evidenceId}
      data-evidence-id={evidenceId}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      inputMode={inputMode}
      required={required}
      aria-required={required || undefined}
      aria-invalid={invalid}
      aria-describedby={describedBy}
      maxLength={maxLength}
      pattern={pattern}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: `1px solid ${invalid ? "var(--danger)" : "var(--hair-strong)"}`,
        borderRadius: 6,
        padding: "8px 10px",
      }}
    />
  );
}

function TextArea({
  value,
  onChange,
  placeholder,
  evidenceId,
  inputRef,
  required,
  describedBy,
  invalid = false,
  maxLength,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  evidenceId?: string;
  inputRef?: React.RefObject<HTMLTextAreaElement | null>;
  required?: boolean;
  describedBy?: string;
  invalid?: boolean;
  maxLength?: number;
}) {
  return (
    <textarea
      ref={inputRef}
      id={evidenceId}
      data-evidence-id={evidenceId}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={8}
      spellCheck={false}
      required={required}
      aria-required={required || undefined}
      aria-invalid={invalid}
      aria-describedby={describedBy}
      maxLength={maxLength}
      style={{
        width: "100%",
        boxSizing: "border-box",
        resize: "vertical",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        lineHeight: 1.75,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: `1px solid ${invalid ? "var(--danger)" : "var(--hair-strong)"}`,
        borderRadius: 6,
        padding: "10px 12px",
      }}
    />
  );
}

function DetailPlaceholder() {
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--text-faint)",
        fontSize: 13,
      }}
    >
      Select a template to inspect.
    </div>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ padding: 18, fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
      {children}
    </div>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>{children}</div>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 8,
        padding: "10px 12px",
      }}
    >
      <span
        style={{
          fontSize: 12.5,
          color: "var(--danger)",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {message}
      </span>
      <Button variant="danger" onClick={onRetry} style={{ flexShrink: 0 }}>
        Retry
      </Button>
    </div>
  );
}
