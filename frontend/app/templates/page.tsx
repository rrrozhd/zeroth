"use client";

import { useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  Input,
  Mono,
  NotConnected,
  Skeleton,
  Textarea,
  PageHeader,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  createTemplate,
  deleteTemplate,
  errMsg,
  listTemplates,
} from "@/app/lib/api";

export default function TemplatesPage() {
  const connected = useConnected();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Prompt templates"
        subtitle="Reusable, versioned prompt templates referenced by agent nodes."
      />
      {!connected ? <NotConnected /> : <TemplatesCard />}
    </div>
  );
}

function TemplatesCard() {
  const { data, error, loading, reload } = useAsync(listTemplates, []);
  const [name, setName] = useState("");
  const [version, setVersion] = useState("1");
  const [body, setBody] = useState("");
  const [description, setDescription] = useState("");
  const [variables, setVariables] = useState("");
  const [busy, setBusy] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  async function create() {
    if (!name.trim() || !body.trim()) {
      setCreateErr("Name and template body are required.");
      return;
    }
    setBusy(true);
    setCreateErr(null);
    setCreated(null);
    try {
      const tmpl = await createTemplate({
        name: name.trim(),
        version: Number(version) || 1,
        template_str: body,
        description: description.trim(),
        variables: variables.split(",").map((v) => v.trim()).filter(Boolean),
      });
      setCreated(`${tmpl.name} v${tmpl.version}`);
      setName("");
      setVersion("1");
      setBody("");
      setDescription("");
      setVariables("");
      reload();
    } catch (e) {
      setCreateErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(tname: string, version: number) {
    try {
      await deleteTemplate(tname, version);
    } finally {
      reload();
    }
  }

  const templates = data?.templates ?? [];

  return (
    <Card title="Templates">
      <div className="space-y-5">
        {error && <ApiErrorNote error={error} />}

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">
            Register a template
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Name">
              <Input value={name} onChange={(e) => setName(e.target.value)} className="font-mono" />
            </Field>
            <Field label="Version" hint="bump for a new revision">
              <Input
                type="number"
                min={1}
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                className="font-mono"
              />
            </Field>
            <Field label="Description" hint="optional">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
          </div>
          <Field label="Template body" hint="use {placeholder} for variables">
            <Textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={4}
              className="font-mono text-xs"
            />
          </Field>
          <Field label="Variables" hint="optional — comma-separated placeholder names">
            <Input value={variables} onChange={(e) => setVariables(e.target.value)} className="font-mono" />
          </Field>
          {createErr && <ErrorBox message={createErr} />}
          {created && (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Registered <Mono>{created}</Mono>.
            </p>
          )}
          <Button variant="primary" onClick={create} disabled={busy}>
            {busy ? "Saving…" : "Register template"}
          </Button>
        </div>

        <div className="border-t border-border pt-4">
          {loading && !data && <Skeleton rows={2} />}
          {data && templates.length === 0 && (
            <Empty>No templates yet — register one above.</Empty>
          )}
          {templates.length > 0 && (
            <ul className="space-y-2">
              {templates.map((t) => (
                <li
                  key={`${t.name}@${t.version}`}
                  className="rounded-xl border border-border bg-surface p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <span className="font-medium">{t.name}</span>
                      <span className="ml-2 text-xs text-muted">v{t.version}</span>
                      {t.description && (
                        <p className="mt-0.5 text-xs text-muted">{t.description}</p>
                      )}
                    </div>
                    <Button size="sm" variant="danger" onClick={() => remove(t.name, t.version)}>
                      Delete
                    </Button>
                  </div>
                  <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-zinc-50 p-2 font-mono text-xs leading-relaxed text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
                    {t.template_str}
                  </pre>
                  {t.variables.length > 0 && (
                    <div className="mt-1 text-xs text-muted">
                      Variables: {t.variables.map((v) => `{${v}}`).join(", ")}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}
