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

import { useEffect, useMemo, useState } from "react";
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
  createTemplate,
  deleteTemplateVersion,
  errMsg,
  listTemplates,
  type Template,
  type TemplateList,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/** A stable per-row key: a template name can carry several versions. */
function keyOf(t: Template): string {
  return `${t.name}@${t.version}`;
}

/** Root identifiers referenced as `{{ var }}` in a Jinja2 body, deduped in
 *  first-seen order. Grabs the leading identifier of each `{{ … }}` expression
 *  (so `{{ user.name }}` and `{{ name | upper }}` both surface their root). */
function parseVars(body: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const re = /\{\{[-\s]*([A-Za-z_][A-Za-z0-9_]*)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    const name = m[1];
    if (!seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  }
  return out;
}

/** The variables to show for a template: its declared list, or — when that is
 *  empty — the ones parsed from the body. */
function variablesOf(t: Template): string[] {
  return t.variables.length > 0 ? t.variables : parseVars(t.template_str);
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function TemplatesPage() {
  const templates = useLoad<TemplateList>(listTemplates);

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
      <ListPane
        templates={templates}
        connected={connected}
        mounted={mounted}
        selectedKey={selectedKey}
        onSelect={select}
        onNew={openCreate}
      />
      <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
        {creating ? (
          <CreateForm onCreated={onCreated} onCancel={() => setCreating(false)} />
        ) : selected ? (
          <TemplateDetail key={selectedKey ?? ""} template={selected} onDeleted={onDeleted} />
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
  selectedKey,
  onSelect,
  onNew,
}: {
  templates: Loadable<TemplateList>;
  connected: boolean;
  mounted: boolean;
  selectedKey: string | null;
  onSelect: (t: Template) => void;
  onNew: () => void;
}) {
  const list = templates.data?.templates ?? [];
  return (
    <aside
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
        <Button variant="primary" onClick={onNew} style={{ padding: "4px 9px" }}>
          + New
        </Button>
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
        background: selected ? "rgba(94,234,212,0.07)" : "transparent",
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

function TemplateDetail({ template: t, onDeleted }: { template: Template; onDeleted: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const vars = variablesOf(t);

  async function doDelete() {
    setBusy(true);
    try {
      await deleteTemplateVersion(t.name, String(t.version));
      toast(`Deleted ${t.name}@v${t.version}`);
      onDeleted();
    } catch (e) {
      toast(`Delete failed: ${errMsg(e)}`);
      setBusy(false);
    }
  }

  return (
    <div style={{ padding: "22px 26px", display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Header: name@vN + delete-version */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 17, fontWeight: 600 }}>
          {t.name}
          <span style={{ color: "var(--text-muted)" }}>@v{t.version}</span>
        </span>
        <div style={{ marginLeft: "auto" }}>
          <Button variant="danger" disabled={busy} onClick={doDelete}>
            {busy ? "Deleting…" : "Delete version"}
          </Button>
        </div>
      </div>

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
      <CodeBlock label="Template body (Jinja2)" code={t.template_str} />

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
        color: "var(--agent)",
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
// Create — CreateTemplateRequest is { name, template_str, version, description,
// variables }. version/description/variables carry defaults, so the form asks
// only for name + version + body (+ an optional description); `variables` is
// derived from the body so the stored template declares what it references.
// --------------------------------------------------------------------------

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

  const derivedVars = useMemo(() => parseVars(body), [body]);
  const canSubmit = name.trim().length > 0 && body.trim().length > 0 && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    const parsedVersion = Number(version.trim());
    if (!Number.isInteger(parsedVersion) || parsedVersion < 1) {
      toast("Version must be a whole number ≥ 1.");
      return;
    }

    setBusy(true);
    try {
      const created = await createTemplate({
        name: name.trim(),
        template_str: body,
        version: parsedVersion,
        description: description.trim(),
        variables: derivedVars,
      });
      toast(`Created ${created.name}@v${created.version}`);
      onCreated(created);
    } catch (err) {
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
        A named, versioned Jinja2 prompt template. Variables written as{" "}
        <code style={{ fontFamily: "var(--font-mono)" }}>{"{{ name }}"}</code> in the body are
        detected automatically.
      </p>

      <Card pad={16}>
        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Field label="name" hint="Logical template name (e.g. grounded-answer).">
            <TextInput value={name} onChange={setName} placeholder="my-template" autoFocus />
          </Field>
          <Field label="version" hint="Whole number ≥ 1.">
            <TextInput value={version} onChange={setVersion} placeholder="1" inputMode="numeric" />
          </Field>
          <Field label="description" hint="Optional — a short note shown in the detail view.">
            <TextInput
              value={description}
              onChange={setDescription}
              placeholder="What this template is for"
            />
          </Field>
          <Field label="template_str" hint="The Jinja2 body.">
            <TextArea
              value={body}
              onChange={setBody}
              placeholder={"Answer the question using only the context.\n\nQuestion: {{ question }}"}
            />
          </Field>

          <div>
            <MonoLabel style={{ display: "block", marginBottom: 6 }}>
              Detected variables
            </MonoLabel>
            {derivedVars.length === 0 ? (
              <EmptyInline>None yet — reference one as {"{{ question }}"} in the body.</EmptyInline>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {derivedVars.map((v) => (
                  <VariableChip key={v} name={v} />
                ))}
              </div>
            )}
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <Button type="submit" variant="primary" disabled={!canSubmit}>
              {busy ? "Creating…" : "Create template"}
            </Button>
            <Button type="button" variant="neutral" onClick={onCancel} disabled={busy}>
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
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "block" }}>
      <MonoLabel style={{ display: "block", marginBottom: 5 }}>{label}</MonoLabel>
      {children}
      {hint && (
        <span
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
    </label>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  autoFocus,
  inputMode,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  inputMode?: "numeric";
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      inputMode={inputMode}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
        outline: "none",
      }}
    />
  );
}

function TextArea({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={8}
      spellCheck={false}
      style={{
        width: "100%",
        boxSizing: "border-box",
        resize: "vertical",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        lineHeight: 1.75,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "10px 12px",
        outline: "none",
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
