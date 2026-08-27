import { deleteWebhookSubscription } from "@/app/lib/api";

export const WEBHOOK_EVENT_TYPES = [
  "run.completed",
  "run.failed",
  "approval.requested",
  "approval.resolved",
  "approval.escalated",
] as const;

export function webhookFailureText(statusCode: number | null, error: string | null): string {
  const status = statusCode == null ? null : `HTTP ${statusCode}`;
  if (status && error && error !== status) return `${status} · ${error}`;
  return status ?? error ?? "delivery failed";
}

export async function deactivateConfirmedWebhook(
  id: string,
  targetUrl: string,
  confirm: (message: string) => boolean = window.confirm,
  remove: (id: string) => Promise<unknown> = deleteWebhookSubscription,
): Promise<boolean> {
  if (!confirm(`Deactivate webhook subscription ${id} (${targetUrl})?`)) return false;
  await remove(id);
  return true;
}
