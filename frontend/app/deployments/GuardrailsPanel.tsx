"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card, MonoLabel, Skeleton } from "@/app/components/primitives";
import {
  errMsg,
  getDeploymentGuardrails,
  updateDeploymentGuardrails,
  type GuardrailPolicyPatch,
  type GuardrailPolicyResponse,
} from "@/app/lib/api";

type GuardrailKey = Exclude<keyof GuardrailPolicyPatch, "reset_fields">;
type OverrideValues = Record<GuardrailKey, string>;

const EMPTY_OVERRIDES: OverrideValues = {
  rate_limit_capacity: "",
  rate_limit_refill_rate: "",
  rate_limit_burst: "",
  quota_daily_limit: "",
  backpressure_queue_depth: "",
  max_concurrency: "",
};

const MIN_REFILL_RATE = 1 / 86_400;

const FIELDS: Array<{
  key: GuardrailKey;
  label: string;
  aria: string;
  hint: string;
  min: number;
  max: number;
  integer?: boolean;
}> = [
  {
    key: "rate_limit_capacity",
    label: "Bucket capacity",
    aria: "Capacity override",
    hint: "1–1,000,000 requests",
    min: 1,
    max: 1_000_000,
  },
  {
    key: "rate_limit_refill_rate",
    label: "Refill rate",
    aria: "Refill override",
    hint: "at least 1 token/day, up to 100,000 requests/second",
    min: MIN_REFILL_RATE,
    max: 100_000,
  },
  {
    key: "rate_limit_burst",
    label: "Burst allowance",
    aria: "Burst override",
    hint: "0–1,000,000 extra requests",
    min: 0,
    max: 1_000_000,
  },
  {
    key: "quota_daily_limit",
    label: "Daily quota",
    aria: "Daily quota override",
    hint: "1–1,000,000,000,000 or “unlimited”",
    min: 1,
    max: 1_000_000_000_000,
    integer: true,
  },
  {
    key: "backpressure_queue_depth",
    label: "Queue depth",
    aria: "Queue depth override",
    hint: "1–1,000,000 queued runs",
    min: 1,
    max: 1_000_000,
    integer: true,
  },
  {
    key: "max_concurrency",
    label: "Concurrency",
    aria: "Concurrency override",
    hint: "1–10,000 shared active runs",
    min: 1,
    max: 10_000,
    integer: true,
  },
];

function parseOverrides(values: OverrideValues): GuardrailPolicyPatch {
  const patch: GuardrailPolicyPatch = {};
  const resetFields: GuardrailKey[] = [];
  for (const field of FIELDS) {
    const raw = values[field.key].trim();
    if (!raw) continue;
    if (raw.toLowerCase() === "inherit") {
      resetFields.push(field.key);
      continue;
    }
    if (field.key === "quota_daily_limit" && raw.toLowerCase() === "unlimited") {
      patch.quota_daily_limit = null;
      continue;
    }
    const value = Number(raw);
    if (!Number.isFinite(value) || value < field.min || value > field.max) {
      throw new Error(`${field.label} must be ${field.hint}.`);
    }
    if (field.integer && !Number.isInteger(value)) {
      throw new Error(`${field.label} must be a whole number.`);
    }
    patch[field.key] = value;
  }
  if (resetFields.length > 0) patch.reset_fields = resetFields;
  if (Object.keys(patch).length === 0) {
    throw new Error("Enter at least one deployment override or reset before saving.");
  }
  return patch;
}

export function GuardrailsPanel({ refId }: { refId: string }) {
  const [data, setData] = useState<GuardrailPolicyResponse | null>(null);
  const [values, setValues] = useState<OverrideValues>(EMPTY_OVERRIDES);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const response = await getDeploymentGuardrails(refId);
      setData(response);
      setValues(EMPTY_OVERRIDES);
    } catch (error) {
      setLoadError(errMsg(error));
    } finally {
      setLoading(false);
    }
  }, [refId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaveError(null);
    setSaved(false);
    let patch: GuardrailPolicyPatch;
    try {
      patch = parseOverrides(values);
    } catch (error) {
      setSaveError(errMsg(error));
      return;
    }
    setSaving(true);
    try {
      const response = await updateDeploymentGuardrails(refId, patch);
      setData(response);
      setValues(EMPTY_OVERRIDES);
      setSaved(true);
    } catch (error) {
      setSaveError(errMsg(error));
    } finally {
      setSaving(false);
    }
  }

  if (loading && !data) {
    return <Skeleton height={150} />;
  }
  if (loadError && !data) {
    return (
      <Card label="Ingress guardrails">
        <ErrorNotice message={loadError} />
        <Button onClick={() => void load()} style={{ marginTop: 10 }}>
          Retry
        </Button>
      </Card>
    );
  }
  if (!data) return null;

  const effective = data.effective;
  return (
    <Card label="Ingress guardrails">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 16,
          alignItems: "flex-start",
          flexWrap: "wrap",
        }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Effective settings</div>
          <div style={{ color: "var(--text-muted)", fontSize: 12, marginTop: 4 }}>
            Deployment overrides take precedence over tenant defaults and product defaults.
          </div>
        </div>
        <div
          style={{
            display: "flex",
            gap: "5px 14px",
            flexWrap: "wrap",
            color: "var(--text-secondary)",
            fontFamily: "var(--font-mono)",
            fontSize: 11.5,
          }}
        >
          <span>{effective.rate_limit_capacity} capacity</span>
          <span>{effective.rate_limit_refill_rate}/s refill</span>
          <span>+{effective.rate_limit_burst} burst</span>
          <span>{effective.quota_daily_limit == null ? "unlimited daily" : `${effective.quota_daily_limit} daily`}</span>
          <span>{effective.backpressure_queue_depth} queued</span>
          <span>{effective.max_concurrency} concurrent</span>
        </div>
      </div>

      <form onSubmit={save} style={{ marginTop: 16 }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(185px, 1fr))",
            gap: 10,
          }}
        >
          {FIELDS.map((field) => {
            const overrides = data.deployment_overrides;
            const hasActiveOverride =
              overrides != null && Object.prototype.hasOwnProperty.call(overrides, field.key);
            const activeValue = hasActiveOverride ? overrides[field.key] : undefined;
            const displayedActive =
              field.key === "quota_daily_limit" && activeValue == null
                ? "unlimited"
                : String(activeValue);
            return (
              <label key={field.key} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <MonoLabel>{field.label}</MonoLabel>
                <input
                  aria-label={field.aria}
                  inputMode="text"
                  placeholder="preserve"
                  value={values[field.key]}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [field.key]: event.target.value }))
                  }
                  style={{
                    border: "1px solid var(--hair-strong)",
                    background: "var(--bg-raised)",
                    borderRadius: 6,
                    color: "var(--text-primary)",
                    fontFamily: "var(--font-mono)",
                    fontSize: 12,
                    padding: "8px 9px",
                  }}
                />
                <span style={{ color: "var(--text-faint)", fontSize: 10.5 }}>{field.hint}</span>
                <span style={{ color: "var(--text-muted)", fontSize: 10.5 }}>
                  {hasActiveOverride
                    ? `Active override: ${displayedActive}`
                    : `No active override; inherits ${String(effective[field.key] ?? "unlimited")}`}
                </span>
              </label>
            );
          })}
        </div>
        {saveError && <ErrorNotice message={saveError} />}
        {saved && (
          <div role="status" style={{ color: "var(--success)", fontSize: 12, marginTop: 12 }}>
            Saved immutable revision.
          </div>
        )}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14 }}>
          <Button type="submit" variant="primary" disabled={saving}>
            {saving ? "Saving…" : "Save overrides"}
          </Button>
          <Button type="button" disabled={saving} onClick={() => setValues(EMPTY_OVERRIDES)}>
            Clear form
          </Button>
          <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
            Blank preserves. “inherit” resets to tenant/configured defaults. A number sets an
            override; daily quota also accepts “unlimited”.
          </span>
        </div>
      </form>
    </Card>
  );
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div role="alert" style={{ color: "var(--danger)", fontSize: 12, marginTop: 12 }}>
      {message} Check deployment-admin permissions and API connectivity, then retry.
    </div>
  );
}
