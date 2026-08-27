"use client";

import { useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  fmtTime,
  Input,
  Mono,
  NotConnected,
  PageHeader,
  Skeleton,
  StatusBadge,
  Textarea,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import { usePolling } from "@/app/hooks/usePolling";
import {
  ApiError,
  claimRepoInstallation,
  createRepoCheckout,
  createRepoRun,
  errMsg,
  getRepoRunEvidence,
  getRepoRun,
  listInstallationRepositories,
  listRepoInstallations,
  resolveRepoRef,
  type RepoCheckout,
  type RepoInstallation,
  type RepoRepository,
  type RepoRun,
  type RepoRunEvidence,
  type RepoValidationIssue,
} from "@/app/lib/api";

// Terminal repo-run states (RepoRunState in repo_models.py): polling stops and
// the evidence bundle becomes fetchable once one of these is reached.
const TERMINAL_RUN_STATES = new Set(["succeeded", "failed"]);

/** The manifest-validation issue list off a checkout-creation 422, when the
    detail carries one (`manifest_validation_failed`); null for every other
    failure shape so callers fall back to the plain message. */
export function checkoutIssues(e: unknown): RepoValidationIssue[] | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  const issues = (e.detail as { issues?: unknown } | null)?.issues;
  return Array.isArray(issues) ? (issues as RepoValidationIssue[]) : null;
}

export default function ReposPage() {
  const connected = useConnected();
  // Selection flows downhill: installation -> repository -> checkout -> run.
  // Changing an upstream selection invalidates everything below it.
  const [installation, setInstallation] = useState<RepoInstallation | null>(null);
  const [repo, setRepo] = useState<RepoRepository | null>(null);
  const [checkout, setCheckout] = useState<RepoCheckout | null>(null);
  const [run, setRun] = useState<RepoRun | null>(null);

  function selectInstallation(next: RepoInstallation) {
    setInstallation(next);
    setRepo(null);
    setCheckout(null);
    setRun(null);
  }
  function selectRepo(next: RepoRepository) {
    setRepo(next);
    setCheckout(null);
    setRun(null);
  }
  function stageCheckout(next: RepoCheckout) {
    setCheckout(next);
    setRun(null);
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Repositories"
        subtitle="GitHub installations, governed checkouts, and declared-script runs."
      />
      {!connected ? (
        <NotConnected />
      ) : (
        <>
          <InstallationsCard
            selectedInstallation={installation}
            selectedRepo={repo}
            onSelectInstallation={selectInstallation}
            onSelectRepo={selectRepo}
          />
          <CheckoutCard repo={repo} checkout={checkout} onCheckout={stageCheckout} />
          <RunCard checkout={checkout} run={run} onRun={setRun} />
          <ProvenanceCard run={run} />
        </>
      )}
    </div>
  );
}

function InstallationsCard({
  selectedInstallation,
  selectedRepo,
  onSelectInstallation,
  onSelectRepo,
}: {
  selectedInstallation: RepoInstallation | null;
  selectedRepo: RepoRepository | null;
  onSelectInstallation: (installation: RepoInstallation) => void;
  onSelectRepo: (repo: RepoRepository) => void;
}) {
  const { data, error, loading, reload } = useAsync(listRepoInstallations, []);
  const installations = data ?? [];
  const [claimId, setClaimId] = useState("");
  const [claiming, setClaiming] = useState(false);
  const [claimErr, setClaimErr] = useState<string | null>(null);

  async function claim() {
    const id = Number(claimId.trim());
    if (!claimId.trim() || !Number.isInteger(id) || id <= 0) {
      setClaimErr("Installation id must be a positive integer.");
      return;
    }
    setClaiming(true);
    setClaimErr(null);
    try {
      await claimRepoInstallation(id);
      setClaimId("");
      reload();
    } catch (e) {
      setClaimErr(errMsg(e));
    } finally {
      setClaiming(false);
    }
  }

  return (
    <Card
      title="Installations"
      actions={
        <Button onClick={() => reload()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      }
    >
      <div className="space-y-5">
        {error && <ApiErrorNote error={error} />}

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">
            Claim an installation
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <Field label="Installation id" hint="from the GitHub App install">
                <Input
                  value={claimId}
                  onChange={(e) => setClaimId(e.target.value)}
                  placeholder="e.g. 43512286"
                  className="font-mono"
                />
              </Field>
            </div>
            <Button variant="primary" onClick={claim} disabled={claiming}>
              {claiming ? "Claiming…" : "Claim"}
            </Button>
          </div>
          {claimErr && <ErrorBox message={`Claim failed: ${claimErr}`} />}
        </div>

        <div className="border-t border-border pt-4">
          {loading && !data && <Skeleton rows={2} />}
          {data && installations.length === 0 && (
            <Empty>No installations yet — claim one above after installing the GitHub App.</Empty>
          )}
          {installations.length > 0 && (
            <ul className="divide-y divide-border">
              {installations.map((inst) => (
                <li key={inst.installation_id} className="py-2.5 text-sm first:pt-0">
                  <button
                    type="button"
                    onClick={() => onSelectInstallation(inst)}
                    className={`flex w-full items-center justify-between gap-3 rounded-lg px-2 py-1 text-left transition-colors hover:bg-zinc-50 dark:hover:bg-zinc-800/60 ${
                      selectedInstallation?.installation_id === inst.installation_id
                        ? "bg-zinc-100 dark:bg-zinc-800/80"
                        : ""
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="truncate font-medium">{inst.account_login}</div>
                      <div className="mt-0.5 text-xs text-muted">
                        <Mono>{String(inst.installation_id)}</Mono> · {inst.account_type} ·{" "}
                        {inst.repository_selection} repositories · claimed{" "}
                        {fmtTime(inst.created_at)}
                      </div>
                    </div>
                    <StatusBadge status={inst.status} />
                  </button>
                  {selectedInstallation?.installation_id === inst.installation_id && (
                    <InstallationRepos
                      key={inst.installation_id}
                      installation={inst}
                      selectedRepo={selectedRepo}
                      onSelectRepo={onSelectRepo}
                    />
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

function InstallationRepos({
  installation,
  selectedRepo,
  onSelectRepo,
}: {
  installation: RepoInstallation;
  selectedRepo: RepoRepository | null;
  onSelectRepo: (repo: RepoRepository) => void;
}) {
  const { data, error, loading } = useAsync(
    () => listInstallationRepositories(installation.installation_id),
    [installation.installation_id],
  );
  const repos = data ?? [];

  return (
    <div className="ml-2 mt-2 border-l border-border pl-4">
      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={1} />}
      {data && repos.length === 0 && (
        <Empty>No repository grants under this installation.</Empty>
      )}
      {repos.length > 0 && (
        <ul className="divide-y divide-border">
          {repos.map((r) => (
            <li key={r.repository_id} className="py-1.5 text-sm first:pt-0">
              <button
                type="button"
                onClick={() => onSelectRepo(r)}
                className={`flex w-full items-center justify-between gap-3 rounded-lg px-2 py-1 text-left transition-colors hover:bg-zinc-50 dark:hover:bg-zinc-800/60 ${
                  selectedRepo?.repository_id === r.repository_id
                    ? "bg-zinc-100 dark:bg-zinc-800/80"
                    : ""
                }`}
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs">{r.full_name}</div>
                  <div className="mt-0.5 text-xs text-muted">
                    default {r.default_branch} · {r.private ? "private" : "public"}
                  </div>
                </div>
                <StatusBadge status={r.status} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CheckoutCard({
  repo,
  checkout,
  onCheckout,
}: {
  repo: RepoRepository | null;
  checkout: RepoCheckout | null;
  onCheckout: (checkout: RepoCheckout) => void;
}) {
  return (
    <Card title="Checkout">
      {!repo ? (
        <Empty>Select a repository above to stage a governed checkout.</Empty>
      ) : (
        <CheckoutForm key={repo.repository_id} repo={repo} checkout={checkout} onCheckout={onCheckout} />
      )}
    </Card>
  );
}

function CheckoutForm({
  repo,
  checkout,
  onCheckout,
}: {
  repo: RepoRepository;
  checkout: RepoCheckout | null;
  onCheckout: (checkout: RepoCheckout) => void;
}) {
  const [ref, setRef] = useState("");
  const [sha, setSha] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [issues, setIssues] = useState<RepoValidationIssue[] | null>(null);

  async function resolve() {
    const value = ref.trim();
    if (!value) {
      setErr("Enter a branch, tag, or commit SHA to resolve.");
      return;
    }
    setResolving(true);
    setErr(null);
    try {
      setSha((await resolveRepoRef(repo.repository_id, value)).commit_sha);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setResolving(false);
    }
  }

  async function create() {
    const value = ref.trim();
    if (!value && !sha) {
      setErr("Enter a ref (or resolve one to a commit SHA) first.");
      return;
    }
    setCreating(true);
    setErr(null);
    setIssues(null);
    try {
      // A resolved SHA pins the checkout to exactly what was shown; otherwise
      // the backend resolves the ref itself (exactly one of the two is sent).
      onCheckout(
        await createRepoCheckout(
          repo.repository_id,
          sha ? { commit_sha: sha } : { ref: value },
        ),
      );
    } catch (e) {
      const found = checkoutIssues(e);
      if (found) {
        setIssues(found);
        setErr("Manifest validation failed — the checkout was refused:");
      } else {
        setErr(errMsg(e));
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="text-xs text-muted">
        Staging from <Mono>{repo.full_name}</Mono>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-72">
          <Field label="Ref" hint="branch, tag, or 40-hex commit SHA">
            <Input
              value={ref}
              onChange={(e) => {
                setRef(e.target.value);
                setSha(null); // a resolved pin is stale once the ref changes
              }}
              placeholder={repo.default_branch}
              className="font-mono"
            />
          </Field>
        </div>
        <Button onClick={resolve} disabled={resolving}>
          {resolving ? "Resolving…" : "Resolve"}
        </Button>
        <Button variant="primary" onClick={create} disabled={creating}>
          {creating ? "Creating…" : "Create checkout"}
        </Button>
      </div>
      {sha && (
        <div className="text-sm">
          <span className="text-muted">Pinned to </span>
          <Mono>{sha}</Mono>
        </div>
      )}
      {err && <ErrorBox message={err} />}
      {issues && <ValidationIssues issues={issues} />}

      {checkout && (
        <div className="space-y-2 border-t border-border pt-4">
          <div className="flex items-center gap-3">
            <StatusBadge status={checkout.state} />
            <span className="text-xs text-muted">
              checkout <Mono>{checkout.checkout_id}</Mono> · created{" "}
              {fmtTime(checkout.created_at)}
            </span>
          </div>
          <dl className="space-y-1 text-sm">
            <DigestRow label="Commit SHA" value={checkout.resolved_commit_sha} />
            <DigestRow label="Tree digest" value={checkout.tree_digest} />
            <DigestRow label="Config digest" value={checkout.config_digest} />
            <DigestRow label="Script" value={checkout.script_name} />
          </dl>
          {checkout.validation_report && checkout.validation_report.length > 0 && (
            <ValidationIssues issues={checkout.validation_report} />
          )}
        </div>
      )}
    </div>
  );
}

function DigestRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-28 shrink-0 text-xs text-muted">{label}</dt>
      <dd className="min-w-0 break-all">
        {value != null ? <Mono>{value}</Mono> : <span className="text-muted">—</span>}
      </dd>
    </div>
  );
}

function ValidationIssues({ issues }: { issues: RepoValidationIssue[] }) {
  return (
    <ul className="space-y-1.5">
      {issues.map((issue, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-2 text-sm">
          <span
            className={`rounded-full px-2 py-0.5 font-mono text-xs ${
              issue.severity === "error"
                ? "bg-red-500/12 text-red-700 dark:text-red-400"
                : "bg-amber-500/15 text-amber-700 dark:text-amber-400"
            }`}
          >
            {issue.code}
          </span>
          {issue.path.length > 0 && <Mono>{issue.path.join(".")}</Mono>}
          <span className="text-muted">{issue.message}</span>
        </li>
      ))}
    </ul>
  );
}

function RunCard({
  checkout,
  run,
  onRun,
}: {
  checkout: RepoCheckout | null;
  run: RepoRun | null;
  onRun: (run: RepoRun) => void;
}) {
  const [payload, setPayload] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pollErr, setPollErr] = useState<string | null>(null);

  const runId = run?.run_id ?? null;
  const polling = run != null && !TERMINAL_RUN_STATES.has(run.state);
  usePolling(
    () => {
      if (!runId) return;
      getRepoRun(runId)
        .then((next) => {
          setPollErr(null);
          onRun(next);
        })
        .catch((e) => setPollErr(errMsg(e)));
    },
    3000,
    polling,
  );

  async function runScript() {
    if (!checkout?.script_name) return;
    let parsed: unknown;
    try {
      parsed = payload.trim() ? JSON.parse(payload) : {};
    } catch (e) {
      setErr(`Input isn't valid JSON: ${(e as Error).message}`);
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setErr('Input must be a JSON object — e.g. {"key": "value"}.');
      return;
    }
    setBusy(true);
    setErr(null);
    setPollErr(null);
    try {
      onRun(
        await createRepoRun(checkout.checkout_id, {
          script: checkout.script_name,
          input_payload: parsed as Record<string, unknown>,
        }),
      );
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Run">
      {!checkout ? (
        <Empty>Stage a checkout above to run its declared script.</Empty>
      ) : (
        <div className="space-y-4">
          <div className="text-sm">
            <span className="text-muted">Declared script: </span>
            {checkout.script_name ? (
              <Mono>{checkout.script_name}</Mono>
            ) : (
              <span className="text-muted">none — this checkout declares no runnable script.</span>
            )}
          </div>
          <Field label="Input payload" hint="JSON object handed to the script">
            <Textarea
              value={payload}
              onChange={(e) => setPayload(e.target.value)}
              rows={4}
              className="font-mono"
            />
          </Field>
          {err && <ErrorBox message={err} />}
          <Button
            variant="primary"
            onClick={runScript}
            disabled={busy || !checkout.script_name}
          >
            {busy ? "Submitting…" : "Run"}
          </Button>

          {run && (
            <div className="space-y-2 border-t border-border pt-4">
              <div className="flex items-center gap-3">
                <StatusBadge status={run.state} />
                {polling && <span className="text-xs text-muted">polling…</span>}
                <span className="text-xs text-muted">
                  run <Mono>{run.run_id}</Mono>
                </span>
              </div>
              <div className="flex flex-wrap gap-6 text-sm">
                <div>
                  <span className="text-muted">Exit code: </span>
                  {run.exit_code != null ? <Mono>{String(run.exit_code)}</Mono> : "—"}
                </div>
                <div>
                  <span className="text-muted">Smoke passed: </span>
                  {run.smoke_passed == null ? "—" : run.smoke_passed ? "yes" : "no"}
                </div>
                {run.failure_code && (
                  <div>
                    <span className="text-muted">Failure: </span>
                    <Mono>{run.failure_code}</Mono>
                  </div>
                )}
              </div>
              {pollErr && <ErrorBox message={`Status refresh failed: ${pollErr}`} />}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function ProvenanceCard({ run }: { run: RepoRun | null }) {
  const finished = run != null && TERMINAL_RUN_STATES.has(run.state);
  const runId = finished ? run.run_id : null;
  const { data, error, loading, reload } = useAsync<RepoRunEvidence | null>(
    () => (runId ? getRepoRunEvidence(runId) : Promise.resolve(null)),
    [runId],
  );

  return (
    <Card
      title="Provenance"
      actions={
        runId ? (
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        ) : undefined
      }
    >
      {!runId ? (
        <Empty>Evidence appears here once a run finishes.</Empty>
      ) : (
        <div className="space-y-5">
          {error && <ApiErrorNote error={error} />}
          {loading && !data && <Skeleton rows={3} />}
          {data && (
            <>
              <div className="space-y-2">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Checkout attestation
                </div>
                {data.checkout_attestation ? (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                          data.checkout_attestation.verified
                            ? "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400"
                            : "bg-red-500/12 text-red-700 dark:text-red-400"
                        }`}
                      >
                        {data.checkout_attestation.verified ? "verified" : "verification failed"}
                      </span>
                      <span className="text-xs text-muted">
                        digest {data.checkout_attestation.digest_verified ? "ok" : "mismatch"} ·
                        signature{" "}
                        {data.checkout_attestation.signature_verified == null
                          ? "unsigned"
                          : data.checkout_attestation.signature_verified
                            ? "ok"
                            : "invalid"}
                      </span>
                    </div>
                    <dl className="space-y-1 text-sm">
                      <DigestRow
                        label="Commit SHA"
                        value={data.checkout_attestation.payload.commit_sha}
                      />
                      <DigestRow
                        label="Tree digest"
                        value={data.checkout_attestation.payload.tree_digest}
                      />
                      <DigestRow
                        label="Config digest"
                        value={data.checkout_attestation.payload.config_digest}
                      />
                      <DigestRow
                        label="Attestation"
                        value={data.checkout_attestation.attestation_digest}
                      />
                    </dl>
                  </div>
                ) : (
                  <Empty>No attestation recorded for this checkout.</Empty>
                )}
              </div>

              <div className="space-y-2 border-t border-border pt-4">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Audit summary
                </div>
                <div className="flex flex-wrap gap-6 text-sm">
                  <SummaryStat label="Audit records" value={data.summary.audit_count} />
                  <SummaryStat label="Tool calls" value={data.summary.tool_call_count} />
                  <SummaryStat
                    label="Memory interactions"
                    value={data.summary.memory_interaction_count}
                  />
                  <SummaryStat label="Approvals" value={data.summary.approval_count} />
                </div>
              </div>

              <div className="space-y-2 border-t border-border pt-4">
                <div className="text-xs font-semibold uppercase tracking-wide text-muted">
                  Policy events
                </div>
                {data.policy_events.length === 0 ? (
                  <Empty>No policy events were recorded for this run.</Empty>
                ) : (
                  <ul className="space-y-1 text-sm">
                    {data.policy_events.map((event, i) => (
                      <li key={i}>
                        <Mono>{event}</Mono>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function SummaryStat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  );
}
