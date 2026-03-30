import { z } from "zod";

const validationIssueSchema = z.object({
  severity: z.enum(["warning", "error"]),
  code: z.string(),
  message: z.string(),
  graph_id: z.string(),
  node_id: z.string().nullable(),
  edge_id: z.string().nullable(),
  path: z.array(z.string()),
  details: z.record(z.string(), z.unknown()),
});

const validationReportSchema = z.object({
  graph_id: z.string(),
  issues: z.array(validationIssueSchema),
});

const contractLookupSchema = z.object({
  name: z.string(),
  version: z.number().int(),
  json_schema: z.record(z.string(), z.unknown()),
});

export type GraphValidationIssue = z.infer<typeof validationIssueSchema>;
export type GraphValidationReport = z.infer<typeof validationReportSchema>;
export type ContractLookup = z.infer<typeof contractLookupSchema>;

type FetchLike = typeof fetch;

export interface ValidationApiClientOptions {
  baseUrl?: string;
  fetch?: FetchLike;
  headers?: HeadersInit;
}

export class ValidationApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly headers: HeadersInit;

  constructor(options: ValidationApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.fetchImpl = options.fetch ?? fetch;
    this.headers = options.headers ?? {};
  }

  async validateWorkflow(workflowId: string): Promise<GraphValidationReport> {
    return this.request(`/studio/workflows/${encodeURIComponent(workflowId)}/validate`, {
      method: "POST",
      schema: validationReportSchema,
    });
  }

  async lookupContract(contractRef: string): Promise<ContractLookup> {
    return this.request(`/studio/contracts/${encodeURIComponent(contractRef)}`, {
      method: "GET",
      schema: contractLookupSchema,
    });
  }

  private async request<TSchema extends z.ZodTypeAny>(
    path: string,
    options: {
      method: string;
      schema: TSchema;
    },
  ): Promise<z.infer<TSchema>> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: options.method,
      headers: {
        Accept: "application/json",
        ...this.headers,
      },
    });

    if (!response.ok) {
      throw new Error(`Validation API request failed with status ${response.status}`);
    }

    return options.schema.parse(await response.json());
  }
}

export function createValidationApiClient(options?: ValidationApiClientOptions): ValidationApiClient {
  return new ValidationApiClient(options);
}
