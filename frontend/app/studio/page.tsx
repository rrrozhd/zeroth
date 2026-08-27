"use client";

// The Studio list screen — author and manage workflow graphs, wired to the live
// Studio API. Presented as an operational drafting ledger;
// the data flow is unchanged: graphs come from `listWorkflows`, a row opens the
// canvas at `/studio/edit?id=…`, "Start from scratch" creates a draft via
// `createWorkflow`, and each template instantiates an editable draft via
// `instantiateTemplate`. The API key lives only in localStorage — never logged,
// never placed in a URL (rows carry the workflow id alone).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  Button,
  ConsoleDataList,
  ConsoleDataRow,
  ConsoleEmpty,
  ConsoleField,
  ConsoleInput,
  ConsoleMeta,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  Skeleton,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  createWorkflow,
  deleteWorkflow,
  errMsg,
  listWorkflows,
  type WorkflowSummary,
} from "@/app/lib/api";
import {
  instantiateTemplate,
  WORKFLOW_TEMPLATES,
  type WorkflowTemplate,
} from "@/app/lib/templates";
import styles from "./studio.module.css";

// Graph lifecycle -> its state color (handoff §2): deployed teal, published
// green, draft muted; anything unmapped degrades to muted.
const LIFECYCLE_COLOR: Record<string, string> = {
  deployed: "var(--accent)",
  published: "var(--success)",
  draft: "var(--text-muted)",
};

function lifecycleColor(status: string): string {
  return LIFECYCLE_COLOR[status?.toLowerCase()] ?? "var(--text-muted)";
}

export default function StudioPage() {
  const router = useRouter();
  const toast = useToast();
  const { data, error, loading, reload } = useLoad<WorkflowSummary[]>(listWorkflows);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; msg: string } | null>(null);

  async function create() {
    if (!name.trim()) return;
    const label = name.trim();
    setBusy(true);
    setFormError(null);
    try {
      const created = await createWorkflow(label);
      setName("");
      toast(`Created draft "${label}"`);
      router.push(`/studio/edit?id=${encodeURIComponent(created.id)}`);
    } catch (e) {
      setFormError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, label: string) {
    if (!window.confirm(`Delete "${label}"? This cannot be undone.`)) return;
    setDeletingId(id);
    setRowError(null);
    try {
      await deleteWorkflow(id);
      toast(`Deleted "${label}"`);
      reload();
    } catch (e) {
      setRowError({ id, msg: errMsg(e) });
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Studio"
        description="Compose, inspect, and issue governed workflow graphs. Published records are immutable."
        actions={
          <>
            <Button variant="neutral" onClick={() => reload()} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </Button>
            <Button
              variant="primary"
              onClick={() => document.getElementById("new-workflow-name")?.focus()}
            >
              New record
            </Button>
          </>
        }
      />

      {/* Graph list — the primary content of the Studio page. */}
      <ConsoleSection
        title="Workflows"
        meta={data ? `${data.length} record${data.length === 1 ? "" : "s"}` : undefined}
      >
        {error && <ApiErrorNote error={error} />}
        {loading && !data && (
          <div className={styles.loadingStack}>
            <Skeleton height={44} />
            <Skeleton height={44} />
            <Skeleton height={44} />
          </div>
        )}
        {data && data.length === 0 && (
          <ConsoleEmpty>No workflows yet. Pick a template below or create a blank workflow.</ConsoleEmpty>
        )}

        {data && data.length > 0 && (
          <ConsoleDataList ariaLabel="Workflows">
            {data.map((w) => (
              <GraphRow
                key={w.id}
                w={w}
                deleting={deletingId === w.id}
                rowError={rowError?.id === w.id ? rowError.msg : null}
                onDelete={() => remove(w.id, w.name)}
              />
            ))}
          </ConsoleDataList>
        )}
      </ConsoleSection>

      {/* Start something new. */}
      <TemplateGallery />

      <ConsoleSection title="Create blank workflow" meta="Starts with an empty canvas">
        <ConsoleSurface>
          <div className={styles.blankForm}>
            <ConsoleField
              label="Workflow name"
              hint="You can rename it later in the editor."
              className={styles.blankField}
            >
              <ConsoleInput
                value={name}
                id="new-workflow-name"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && create()}
                placeholder="Incident review workflow"
              />
            </ConsoleField>
            <Button variant="primary" onClick={create} disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create"}
            </Button>
          </div>
          {formError && (
            <div className={styles.formError}>
              <ConsoleNotice tone="danger" title="Could not create workflow">
                {formError}
              </ConsoleNotice>
            </div>
          )}
        </ConsoleSurface>
      </ConsoleSection>
    </ConsolePage>
  );
}

// A single graph row: name + id (opens the canvas), factual lifecycle state,
// version, and Edit / Delete controls.
function GraphRow({
  w,
  deleting,
  rowError,
  onDelete,
}: {
  w: WorkflowSummary;
  deleting: boolean;
  rowError: string | null;
  onDelete: () => void;
}) {
  const href = `/studio/edit?id=${encodeURIComponent(w.id)}`;
  const stateColor = lifecycleColor(w.status);
  const canDelete = w.status !== "published";

  return (
    <ConsoleDataRow>
      <div className={styles.recordBody} data-evidence-scope={`workflow-${w.id}`}>
        <Link href={href} className={styles.recordLink}>
          <div className={styles.recordName}>{w.name}</div>
          <div className={styles.recordId}>{w.id}</div>
        </Link>

        <span className={styles.recordState} style={{ color: stateColor }}>
          {w.status}
        </span>
        <ConsoleMeta>v{w.version}</ConsoleMeta>
        <Link href={href} className={styles.recordAction}>
          Edit
        </Link>
        {canDelete && (
          <Button variant="danger" onClick={onDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        )}
      </div>
      {rowError && (
        <div className={styles.rowError}>
          <ConsoleNotice tone="danger" title="Could not delete workflow">
            {rowError}
          </ConsoleNotice>
        </div>
      )}
    </ConsoleDataRow>
  );
}

// Ready-made example graphs so a first workflow doesn't start from a blank
// canvas. Instantiating creates a normal draft and jumps straight to the editor.
function TemplateGallery() {
  const router = useRouter();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function use(t: WorkflowTemplate) {
    setBusyId(t.id);
    setError(null);
    try {
      const wf = await instantiateTemplate(t);
      router.push(`/studio/edit?id=${encodeURIComponent(wf.id)}`);
    } catch (e) {
      setError(errMsg(e));
      setBusyId(null);
    }
  }

  return (
    <ConsoleSection title="Start from a template" meta={`${WORKFLOW_TEMPLATES.length} templates`}>
      {error && (
        <div className={styles.formError}>
          <ConsoleNotice tone="danger" title="Could not create from template">
            {error}
          </ConsoleNotice>
        </div>
      )}
      <ConsoleDataList ariaLabel="Workflow templates">
        {WORKFLOW_TEMPLATES.map((t) => (
          <TemplateRow
            key={t.id}
            template={t}
            busy={busyId !== null}
            busyThis={busyId === t.id}
            onUse={() => use(t)}
          />
        ))}
      </ConsoleDataList>
    </ConsoleSection>
  );
}

// A flat template row: name, plain-language description, and one affordance.
function TemplateRow({
  template: t,
  busy,
  busyThis,
  onUse,
}: {
  template: WorkflowTemplate;
  busy: boolean;
  busyThis: boolean;
  onUse: () => void;
}) {
  return (
    <ConsoleDataRow>
      <button type="button" onClick={onUse} disabled={busy} className={styles.templateRow}>
        <span className={styles.templateName}>{t.name}</span>
        <span className={styles.templateDescription}>{t.description}</span>
        <span className={styles.templateAction}>
          {busyThis ? "Creating…" : "Use template"}
        </span>
      </button>
    </ConsoleDataRow>
  );
}

// Friendly, code-aware copy for a load error (ported from the legacy ui.tsx
// ApiErrorNote so this page keeps the same UX without depending on ui.tsx).
function ApiErrorNote({ error }: { error: string }) {
  if (error.startsWith("403")) {
    return (
      <ConsoleNotice tone="danger" title="Permission required">
        This API key cannot view Studio. Connect with a workflow administrator credential.
      </ConsoleNotice>
    );
  }
  if (error.startsWith("401")) {
    return (
      <ConsoleNotice tone="danger" title="Not authenticated">
        Check the API key under Connect and retry.
      </ConsoleNotice>
    );
  }
  if (error.startsWith("0 ") || error.toLowerCase().includes("network error")) {
    return (
      <ConsoleNotice tone="danger" title="Studio API unreachable">
        Check the base URL and confirm the service is running.
      </ConsoleNotice>
    );
  }
  if (error.startsWith("503")) {
    return (
      <ConsoleNotice tone="danger" title="Studio unavailable">
        Configure Studio for this deployment and retry.
      </ConsoleNotice>
    );
  }
  return (
    <ConsoleNotice tone="danger" title="Could not load Studio">
      {error}
    </ConsoleNotice>
  );
}
