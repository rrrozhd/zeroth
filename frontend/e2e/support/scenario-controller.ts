import { expect, type APIRequestContext } from "@playwright/test";

import { extractEvidenceIdentities } from "./live-evaluation";

export type ScenarioExpectation = {
  runStatus: "completed" | "failed" | "cancelled" | "paused";
  markerCount: number;
  reexecutionCount: number;
  partialCollectionCount?: number;
  operationStatus?: "ambiguous" | "completed" | "failed";
};

export type ScenarioDefinition = {
  id: string;
  workflow: 1 | 2 | 3;
  expectation: ScenarioExpectation;
  uiAction?: "refresh" | "refresh_reject" | "cancel" | "approve" | "reject";
  checkpoint?: "duplicate_submission" | "advance_sla" | "restart_before_receipt" | "restart_after_receipt";
  deterministicProviderFault?: boolean;
};

export type PreparedScenario = {
  fixture_id: string;
  input_payload: Record<string, unknown>;
  approval_node_id?: string;
};

export type VerifiedScenario = {
  run_status: string;
  marker_count: number;
  reexecution_count: number;
  partial_collection_count?: number;
  operation_status?: string;
  [key: string]: unknown;
};

export class ScenarioController {
  constructor(
    private readonly request: APIRequestContext,
    private readonly baseUrl: string,
    private readonly controllerKey?: string,
  ) {}

  private headers(): Record<string, string> {
    return this.controllerKey ? { "X-Controller-Key": this.controllerKey } : {};
  }

  async available(): Promise<boolean> {
    try {
      const response = await this.request.get(`${this.baseUrl}/health`, {
        headers: this.headers(),
        timeout: 3_000,
      });
      return response.status() >= 200 && response.status() < 300;
    } catch {
      return false;
    }
  }

  async prepare(definition: ScenarioDefinition, workflowId: string): Promise<PreparedScenario> {
    const response = await this.request.post(`${this.baseUrl}/v1/scenarios/prepare`, {
      headers: this.headers(),
      data: {
        scenario_id: definition.id,
        workflow_id: workflowId,
        expected: {
          run_status: definition.expectation.runStatus,
          marker_count: definition.expectation.markerCount,
          reexecution_count: definition.expectation.reexecutionCount,
          partial_collection_count: definition.expectation.partialCollectionCount,
          operation_status: definition.expectation.operationStatus,
        },
        deterministic_provider_fault: definition.deterministicProviderFault ?? false,
      },
    });
    expect(response.status(), `scenario controller could not prepare ${definition.id}`).toBeGreaterThanOrEqual(200);
    expect(response.status(), `scenario controller could not prepare ${definition.id}`).toBeLessThan(300);
    return await response.json() as PreparedScenario;
  }

  async checkpoint(fixtureId: string, checkpoint: string): Promise<Record<string, unknown>> {
    const response = await this.request.post(
      `${this.baseUrl}/v1/scenarios/${encodeURIComponent(fixtureId)}/checkpoints/${encodeURIComponent(checkpoint)}`,
      { headers: this.headers() },
    );
    expect(response.status(), `scenario checkpoint ${checkpoint} failed`).toBeGreaterThanOrEqual(200);
    expect(response.status(), `scenario checkpoint ${checkpoint} failed`).toBeLessThan(300);
    return await response.json() as Record<string, unknown>;
  }

  async coordinateRestart(fixtureId: string, phase: "before_receipt" | "after_receipt"): Promise<void> {
    // This is intentionally only a controller handshake. Playwright never runs a
    // shell command, stops a process, or decides which service instance to restart.
    await this.checkpoint(fixtureId, `restart_${phase}_ready`);
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const response = await this.request.get(
        `${this.baseUrl}/v1/scenarios/${encodeURIComponent(fixtureId)}/restart-status`,
        { headers: this.headers() },
      );
      if (response.status() >= 200 && response.status() < 300) {
        const body = await response.json() as { state?: string };
        if (body.state === "ready") return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`restart coordination timed out for ${fixtureId}`);
  }

  async verify(fixtureId: string, expectation: ScenarioExpectation): Promise<VerifiedScenario> {
    const deadline = Date.now() + 120_000;
    let body: VerifiedScenario | null = null;
    while (Date.now() < deadline) {
      const response = await this.request.get(
        `${this.baseUrl}/v1/scenarios/${encodeURIComponent(fixtureId)}/verify`,
        { headers: this.headers() },
      );
      if (response.status() >= 200 && response.status() < 300) {
        body = await response.json() as VerifiedScenario;
        if (body.run_status === expectation.runStatus) break;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    expect(body, "scenario controller returned no verification result").not.toBeNull();
    expect(body!.run_status).toBe(expectation.runStatus);
    expect(body!.marker_count).toBe(expectation.markerCount);
    expect(body!.reexecution_count).toBe(expectation.reexecutionCount);
    if (expectation.partialCollectionCount !== undefined) {
      expect(body!.partial_collection_count).toBe(expectation.partialCollectionCount);
    }
    if (expectation.operationStatus !== undefined) {
      expect(body!.operation_status).toBe(expectation.operationStatus);
    }
    expect(extractEvidenceIdentities(body!), "verification response must expose correlation IDs").not.toEqual({});
    return body!;
  }

  async cleanup(fixtureId: string): Promise<void> {
    const response = await this.request.post(
      `${this.baseUrl}/v1/scenarios/${encodeURIComponent(fixtureId)}/cleanup`,
      { headers: this.headers() },
    );
    expect(response.status(), "scenario cleanup failed").toBeGreaterThanOrEqual(200);
    expect(response.status(), "scenario cleanup failed").toBeLessThan(300);
  }
}
