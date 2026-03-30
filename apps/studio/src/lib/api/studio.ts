import { z } from "zod";

const workflowSummarySchema = z.object({
  workflow_id: z.string(),
  workspace_id: z.string(),
  name: z.string(),
  folder_path: z.string(),
  draft_graph_version: z.number().int(),
  validation_status: z.enum(["unknown", "valid", "invalid"]),
});

const workflowGraphSchema = z.object({
  graph_id: z.string(),
  version: z.number().int(),
  entry_step: z.string(),
  nodes: z.array(z.record(z.string(), z.unknown())),
  edges: z.array(z.record(z.string(), z.unknown())),
});

const workflowDetailSchema = z.object({
  workflow_id: z.string(),
  workspace_id: z.string(),
  name: z.string(),
  folder_path: z.string(),
  revision_token: z.string(),
  graph: workflowGraphSchema,
});

const leasePayloadSchema = z.object({
  workflow_id: z.string(),
  workspace_id: z.string(),
  lease_token: z.string(),
  expires_at: z.string(),
});

export type WorkflowSummary = z.infer<typeof workflowSummarySchema>;
export type WorkflowDetail = z.infer<typeof workflowDetailSchema>;
export type LeasePayload = z.infer<typeof leasePayloadSchema>;

type FetchLike = typeof fetch;

export interface StudioApiClientOptions {
  baseUrl?: string;
  fetch?: FetchLike;
  headers?: HeadersInit;
}

export class StudioApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`Studio API request failed with status ${status}`);
    this.name = "StudioApiError";
    this.status = status;
    this.detail = detail;
  }
}

export class StudioApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly headers: HeadersInit;

  constructor(options: StudioApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.fetchImpl = options.fetch ?? fetch;
    this.headers = options.headers ?? {};
  }

  async listWorkflows(): Promise<WorkflowSummary[]> {
    return this.request("/studio/workflows", {
      method: "GET",
      schema: z.array(workflowSummarySchema),
    });
  }

  async getWorkflow(workflowId: string): Promise<WorkflowDetail> {
    return this.request(`/studio/workflows/${encodeURIComponent(workflowId)}`, {
      method: "GET",
      schema: workflowDetailSchema,
    });
  }

  async acquireWorkflowLease(workflowId: string): Promise<LeasePayload> {
    return this.request(`/studio/workflows/${encodeURIComponent(workflowId)}/leases`, {
      method: "POST",
      schema: leasePayloadSchema,
    });
  }

  async renewWorkflowLease(workflowId: string, leaseToken: string): Promise<LeasePayload> {
    return this.request(`/studio/workflows/${encodeURIComponent(workflowId)}/leases/ping`, {
      method: "POST",
      schema: leasePayloadSchema,
      body: JSON.stringify({ lease_token: leaseToken }),
    });
  }

  async releaseWorkflowLease(workflowId: string, leaseToken: string): Promise<void> {
    await this.request(`/studio/workflows/${encodeURIComponent(workflowId)}/leases/${encodeURIComponent(leaseToken)}`, {
      method: "DELETE",
      expectJson: false,
    });
  }

  private async request<TSchema extends z.ZodTypeAny>(
    path: string,
    options: {
      method: string;
      schema?: TSchema;
      body?: BodyInit;
      expectJson?: boolean;
    },
  ): Promise<z.infer<TSchema>> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: options.method,
      body: options.body,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...this.headers,
      },
    });

    if (!response.ok) {
      const detail = await this.readDetail(response);
      throw new StudioApiError(response.status, detail);
    }

    if (options.expectJson === false) {
      return undefined as z.infer<TSchema>;
    }

    const payload: unknown = await response.json();
    if (!options.schema) {
      return payload as z.infer<TSchema>;
    }
    return options.schema.parse(payload);
  }

  private async readDetail(response: Response): Promise<unknown> {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      return response.json();
    }
    return response.text();
  }
}

export function createStudioApiClient(options?: StudioApiClientOptions): StudioApiClient {
  return new StudioApiClient(options);
}
