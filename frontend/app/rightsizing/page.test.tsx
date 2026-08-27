// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  attachQualityVerdict: vi.fn(),
  getRightsizing: vi.fn(),
  getLatestRightsizingExperiment: vi.fn(),
  getRightsizingOpportunities: vi.fn(),
  getUnitEconomics: vi.fn(),
  getWaste: vi.fn(),
  runRightsizingExperiment: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));
vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));

import RightsizingPage from "./page";

describe("measured Rightsizing fields", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const pending = new Promise<never>(() => undefined);
    api.getRightsizingOpportunities.mockReturnValue(pending);
    api.getUnitEconomics.mockReturnValue(pending);
    api.getWaste.mockReturnValue(pending);
    api.getRightsizing.mockReturnValue(pending);
    api.getLatestRightsizingExperiment.mockResolvedValue(null);
    api.runRightsizingExperiment.mockReturnValue(pending);
    api.attachQualityVerdict.mockResolvedValue({});
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.clearAllMocks();
  });

  it("exposes every measured experiment capability and bound", async () => {
    await act(async () => root.render(<RightsizingPage />));

    expect(host.textContent).toContain("Judge model");
    expect(host.textContent).toContain("Maximum candidates");
    expect(host.textContent).toContain("Minimum cases");
    expect(host.textContent).toContain("Candidate needs vision");
  });

  it("leaves the experiment result empty when no completed result is stored", async () => {
    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => expect(api.getLatestRightsizingExperiment).toHaveBeenCalledTimes(1));
    expect(
      host.querySelector('[data-evidence-id="rightsizing.experiment.execution-evidence"]'),
    ).toBeNull();
  });

  it("restores the latest completed experiment result on a fresh mount", async () => {
    api.getLatestRightsizingExperiment.mockResolvedValueOnce({
      incumbent: "openai/gpt-4o-mini",
      node_id: "research",
      mode: "equivalence",
      cases: 1,
      min_cases: 5,
      tolerance_pct: 5,
      incumbent_self_equivalence: 1,
      mean_input_tokens: 42,
      mean_output_tokens: 17,
      token_profile_measured: true,
      harvest: null,
      outcomes: [],
      recommended_model: null,
      verdict: "flagged",
      note: "Restored measured result.",
      execution: {
        run_id: "rightsizing:restored-live-1",
        campaign_id: "campaign-live-1",
        provider_call_count: 4,
        measured_cost_usd: 0.000123,
        estimated_cost_usd: 0.000125,
        calls: [],
      },
    });

    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => expect(api.getLatestRightsizingExperiment).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => {
      const evidence = host.querySelector(
        '[data-evidence-id="rightsizing.experiment.execution-evidence"]',
      );
      expect(evidence?.textContent).toContain("rightsizing:restored-live-1");
      expect(host.textContent).toContain("Restored measured result.");
    });
  });

  it("sends every configured experiment option with its typed value", async () => {
    await act(async () => root.render(<RightsizingPage />));
    const form = Array.from(host.querySelectorAll("form")).find((candidate) =>
      candidate.textContent?.includes("Run experiment"),
    );
    expect(form).toBeTruthy();

    function inputFor(label: string): HTMLInputElement | HTMLTextAreaElement {
      const wrapper = Array.from(form!.querySelectorAll("label")).find((candidate) =>
        candidate.textContent?.startsWith(label),
      );
      const input = wrapper?.querySelector("input, textarea");
      expect(input).toBeTruthy();
      return input as HTMLInputElement | HTMLTextAreaElement;
    }

    async function fill(label: string, value: string) {
      const input = inputFor(label);
      const prototype = input instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      await act(async () => {
        setter?.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }

    await fill("node_id", "answer_node");
    await fill("incumbent", "openai/gpt-4o-mini");
    await fill("instruction", "Answer from the provided evidence.");
    await fill("Tolerance (%)", "100");
    await fill("Maximum cases", "25");
    await fill("Judge model", "openai/gpt-4o-mini");
    await fill("Maximum candidates", "6");
    await fill("Minimum cases", "50");

    const tools = inputFor("Candidate needs tools") as HTMLInputElement;
    const vision = inputFor("Candidate needs vision") as HTMLInputElement;
    await act(async () => {
      tools.click();
      vision.click();
    });
    await act(async () => form!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

    expect(api.runRightsizingExperiment).toHaveBeenCalledWith({
      node_id: "answer_node",
      incumbent: "openai/gpt-4o-mini",
      instruction: "Answer from the provided evidence.",
      needs_tools: true,
      needs_vision: true,
      judge_model: "openai/gpt-4o-mini",
      max_candidates: 6,
      max_cases: 25,
      min_cases: 50,
      tolerance_pct: 100,
      mode: "equivalence",
    });
  });

  it("rejects non-numeric input without starting an experiment", async () => {
    await act(async () => root.render(<RightsizingPage />));
    const form = Array.from(host.querySelectorAll("form")).find((candidate) =>
      candidate.textContent?.includes("Run experiment"),
    )!;
    const inputs = Array.from(form.querySelectorAll("label"));

    async function fill(label: string, value: string) {
      const wrapper = inputs.find((candidate) => candidate.textContent?.startsWith(label));
      const input = wrapper?.querySelector("input, textarea") as HTMLInputElement | HTMLTextAreaElement;
      const prototype = input instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      await act(async () => {
        Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }

    await fill("node_id", "answer_node");
    await fill("incumbent", "openai/gpt-4o-mini");
    await fill("instruction", "Answer from evidence.");
    await fill("Tolerance (%)", "not-a-number");
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

    expect(api.runRightsizingExperiment).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Tolerance must be a number from 0 through 100.");
    const tolerance = inputs
      .find((candidate) => candidate.textContent?.startsWith("Tolerance (%)"))
      ?.querySelector("input");
    expect(tolerance?.getAttribute("aria-invalid")).toBe("true");
    expect(tolerance?.getAttribute("aria-describedby")).toBe("experiment-options-error");
  });

  it("gives every reachable control a stable evidence identity", async () => {
    await act(async () => root.render(<RightsizingPage />));

    const missingBeforeConditional = Array.from(
      host.querySelectorAll<HTMLElement>("button, input, textarea, select"),
    ).filter((control) => !control.dataset.evidenceId);
    expect(missingBeforeConditional.map((control) => control.outerHTML)).toEqual([]);

    const correctness = host.querySelector<HTMLElement>(
      '[data-evidence-id="rightsizing.experiment.mode.correctness"]',
    );
    expect(correctness).toBeTruthy();
    await act(async () => correctness!.click());

    const verdictRegion = host.querySelector(
      '[data-evidence-id="rightsizing.quality-verdict.region"]',
    );
    expect(verdictRegion).toBeTruthy();
    const missingConditional = Array.from(
      verdictRegion!.querySelectorAll<HTMLElement>("button, input, textarea, select"),
    ).filter((control) => !control.dataset.evidenceId);
    expect(missingConditional.map((control) => control.outerHTML)).toEqual([]);
  });

  it("operates the comparison radiogroup with roving focus and arrow keys", async () => {
    await act(async () => root.render(<RightsizingPage />));
    const incumbent = host.querySelector<HTMLButtonElement>(
      '[data-evidence-id="rightsizing.experiment.mode.equivalence"]',
    )!;
    const correctness = host.querySelector<HTMLButtonElement>(
      '[data-evidence-id="rightsizing.experiment.mode.correctness"]',
    )!;

    expect(incumbent.tabIndex).toBe(0);
    expect(correctness.tabIndex).toBe(-1);
    incumbent.focus();
    await act(async () => {
      incumbent.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    });

    expect(correctness.getAttribute("aria-checked")).toBe("true");
    expect(correctness.tabIndex).toBe(0);
    expect(incumbent.tabIndex).toBe(-1);
    expect(document.activeElement).toBe(correctness);
    expect(
      host.querySelector('[data-evidence-id="rightsizing.quality-verdict.region"]'),
    ).toBeTruthy();
  });

  it("associates static-bound errors with the exact invalid field", async () => {
    await act(async () => root.render(<RightsizingPage />));
    const incumbent = host.querySelector<HTMLInputElement>(
      '[data-evidence-id="rightsizing.suggest.incumbent"]',
    )!;
    const savings = host.querySelector<HTMLInputElement>(
      '[data-evidence-id="rightsizing.suggest.min-savings-pct"]',
    )!;
    const submit = host.querySelector<HTMLButtonElement>(
      '[data-evidence-id="rightsizing.suggest.submit"]',
    )!;

    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(incumbent, "gpt-4o");
      incumbent.dispatchEvent(new Event("input", { bubbles: true }));
      setter?.call(savings, "-0.1");
      savings.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => submit.click());

    expect(api.getRightsizing).not.toHaveBeenCalled();
    expect(savings.getAttribute("aria-invalid")).toBe("true");
    expect(savings.getAttribute("aria-describedby")).toBe("suggest-options-error");
    const alert = host.querySelector('[data-evidence-id="rightsizing.suggest.error"]');
    expect(alert?.getAttribute("role")).toBe("alert");
    expect(alert?.textContent).toContain("Minimum savings must be a number from 0 through 100.");
  });

  it("clears each locally owned numeric validation error when its field becomes valid", async () => {
    await act(async () => root.render(<RightsizingPage />));

    async function fill(evidenceId: string, value: string) {
      const input = host.querySelector<HTMLInputElement>(`[data-evidence-id="${evidenceId}"]`)!;
      expect(input).toBeTruthy();
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      return input;
    }

    async function expectOwnedErrorClears({
      evidenceId,
      invalid,
      valid,
      submitEvidenceId,
      errorEvidenceId,
      message,
    }: {
      evidenceId: string;
      invalid: string;
      valid: string;
      submitEvidenceId: string;
      errorEvidenceId: string;
      message: string;
    }) {
      const input = await fill(evidenceId, invalid);
      await act(async () => {
        host.querySelector<HTMLButtonElement>(`[data-evidence-id="${submitEvidenceId}"]`)!.click();
      });

      expect(input.getAttribute("aria-invalid")).toBe("true");
      expect(host.querySelector(`[data-evidence-id="${errorEvidenceId}"]`)?.textContent)
        .toContain(message);

      await fill(evidenceId, valid);

      expect(input.getAttribute("aria-invalid")).not.toBe("true");
      expect(host.querySelector(`[data-evidence-id="${errorEvidenceId}"]`)).toBeNull();
    }

    await fill("rightsizing.suggest.incumbent", "gpt-4o-mini");
    for (const validationCase of [
      {
        evidenceId: "rightsizing.suggest.min-savings-pct",
        invalid: "-0.1",
        valid: "100",
        message: "Minimum savings must be a number from 0 through 100.",
      },
      {
        evidenceId: "rightsizing.suggest.limit",
        invalid: "21",
        valid: "20",
        message: "Limit must be a whole number from 1 through 20.",
      },
    ]) {
      await expectOwnedErrorClears({
        ...validationCase,
        submitEvidenceId: "rightsizing.suggest.submit",
        errorEvidenceId: "rightsizing.suggest.error",
      });
    }

    await fill("rightsizing.experiment.node-id", "research");
    await fill("rightsizing.experiment.incumbent", "gpt-4o-mini");
    const instruction = host.querySelector<HTMLTextAreaElement>(
      '[data-evidence-id="rightsizing.experiment.instruction"]',
    )!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        instruction,
        "Answer only from the provided evidence.",
      );
      instruction.dispatchEvent(new Event("input", { bubbles: true }));
    });
    for (const validationCase of [
      {
        evidenceId: "rightsizing.experiment.tolerance-pct",
        invalid: "-0.1",
        valid: "100",
        message: "Tolerance must be a number from 0 through 100.",
      },
      {
        evidenceId: "rightsizing.experiment.max-cases",
        invalid: "26",
        valid: "25",
        message: "Maximum cases must be a whole number from 1 through 25.",
      },
      {
        evidenceId: "rightsizing.experiment.max-candidates",
        invalid: "7",
        valid: "6",
        message: "Maximum candidates must be a whole number from 1 through 6.",
      },
      {
        evidenceId: "rightsizing.experiment.min-cases",
        invalid: "51",
        valid: "50",
        message: "Minimum cases must be a whole number from 1 through 50.",
      },
    ]) {
      await expectOwnedErrorClears({
        ...validationCase,
        submitEvidenceId: "rightsizing.experiment.submit",
        errorEvidenceId: "rightsizing.experiment.error",
      });
    }

    expect(api.getRightsizing).not.toHaveBeenCalled();
    expect(api.runRightsizingExperiment).not.toHaveBeenCalled();
  });

  it("keeps visible focus styling available on native form controls", async () => {
    await act(async () => root.render(<RightsizingPage />));

    for (const control of host.querySelectorAll<HTMLElement>("input, textarea, select")) {
      expect(control.style.outline, control.outerHTML).not.toBe("none");
    }
  });

  it("reports a correctness verdict submission in-page and exposes all verdict options", async () => {
    await act(async () => root.render(<RightsizingPage />));
    await act(async () => {
      host
        .querySelector<HTMLElement>(
          '[data-evidence-id="rightsizing.experiment.mode.correctness"]',
        )!
        .click();
    });

    const select = host.querySelector<HTMLSelectElement>(
      '[data-evidence-id="rightsizing.quality-verdict.verdict"]',
    )!;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "good",
      "bad",
      "unknown",
    ]);

    const runId = host.querySelector<HTMLInputElement>(
      '[data-evidence-id="rightsizing.quality-verdict.run-id"]',
    )!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(runId, "run-rightsizing-ui-1");
      runId.dispatchEvent(new Event("input", { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(select, "bad");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      host
        .querySelector<HTMLButtonElement>(
          '[data-evidence-id="rightsizing.quality-verdict.submit"]',
        )!
        .click();
    });

    expect(api.attachQualityVerdict).toHaveBeenCalledWith({
      run_id: "run-rightsizing-ui-1",
      source: "human:console",
      verdict: "bad",
      detail: "",
      expected_output: null,
    });
    const feedback = host.querySelector(
      '[data-evidence-id="rightsizing.quality-verdict.feedback"]',
    );
    expect(feedback?.getAttribute("role")).toBe("status");
    expect(feedback?.textContent).toContain('Verdict "bad" attached to run-right');
  });

  it("explains the permission needed when a measured experiment is forbidden", async () => {
    api.runRightsizingExperiment.mockRejectedValueOnce({ status: 403, message: "forbidden" });
    await act(async () => root.render(<RightsizingPage />));
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    const textareaSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;

    await act(async () => {
      for (const [evidenceId, value] of [
        ["rightsizing.experiment.node-id", "research"],
        ["rightsizing.experiment.incumbent", "gpt-4o-mini"],
      ]) {
        const input = host.querySelector<HTMLInputElement>(`[data-evidence-id="${evidenceId}"]`)!;
        setter?.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const instruction = host.querySelector<HTMLTextAreaElement>(
        '[data-evidence-id="rightsizing.experiment.instruction"]',
      )!;
      textareaSetter?.call(instruction, "Answer only from evidence.");
      instruction.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      host
        .querySelector<HTMLButtonElement>('[data-evidence-id="rightsizing.experiment.submit"]')!
        .click();
    });

    await vi.waitFor(() => {
      expect(host.querySelector('[data-evidence-id="rightsizing.experiment.error"]')?.textContent)
        .toContain("Running measured experiments requires Metrics admin permission");
    });
  });

  it("shows the live run and per-call economics returned by a measured experiment", async () => {
    api.runRightsizingExperiment.mockResolvedValueOnce({
      incumbent: "openai/gpt-4o-mini",
      node_id: "research",
      mode: "equivalence",
      cases: 1,
      min_cases: 1,
      tolerance_pct: 5,
      incumbent_self_equivalence: 1,
      mean_input_tokens: 42,
      mean_output_tokens: 17,
      token_profile_measured: true,
      harvest: null,
      outcomes: [],
      recommended_model: null,
      verdict: "none",
      note: "No cheaper candidate met the bar.",
      execution: {
        run_id: "rightsizing:run-live-1",
        campaign_id: "campaign-live-1",
        provider_call_count: 2,
        measured_cost_usd: 0.000123,
        estimated_cost_usd: 0.000125,
        calls: [
          {
            operation_id: "operation-replay-1",
            provider_request_id: "provider-request-1",
            cost_event_id: "cost-event-1",
            audit_event_id: "audit_cost-event-1",
            model: "openai/gpt-4o-mini",
            cost_measurement: "measured",
            measured_cost_usd: 0.000123,
            estimated_cost_usd: 0.000125,
            input_tokens: 42,
            output_tokens: 17,
            cleanup_status: "complete",
            provider_call_attempted: true,
            cache_hit: false,
          },
        ],
      },
    });
    await act(async () => root.render(<RightsizingPage />));
    const inputSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    const textareaSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;

    await act(async () => {
      for (const [evidenceId, value] of [
        ["rightsizing.experiment.node-id", "research"],
        ["rightsizing.experiment.incumbent", "openai/gpt-4o-mini"],
      ]) {
        const input = host.querySelector<HTMLInputElement>(`[data-evidence-id="${evidenceId}"]`)!;
        inputSetter?.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const instruction = host.querySelector<HTMLTextAreaElement>(
        '[data-evidence-id="rightsizing.experiment.instruction"]',
      )!;
      textareaSetter?.call(instruction, "Answer only from evidence.");
      instruction.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      host
        .querySelector<HTMLButtonElement>('[data-evidence-id="rightsizing.experiment.submit"]')!
        .click();
    });

    await vi.waitFor(() => {
      const execution = host.querySelector(
        '[data-evidence-id="rightsizing.experiment.execution-evidence"]',
      );
      expect(execution?.textContent).toContain("rightsizing:run-live-1");
      expect(execution?.textContent).toContain("2 live calls");
      expect(execution?.textContent).toContain("$0.000123 measured");
      expect(execution?.textContent).toContain("$0.000125 estimated");
      expect(execution?.textContent).toContain("provider-request-1");
      expect(execution?.textContent).toContain("cost-event-1");
      expect(execution?.textContent).toContain("complete");
    });
  });

  it("explains metrics-read denial on each read-only economics card", async () => {
    api.getRightsizingOpportunities.mockRejectedValueOnce(new Error("403 forbidden"));
    api.getUnitEconomics.mockRejectedValueOnce(new Error("403 forbidden"));
    api.getWaste.mockRejectedValueOnce(new Error("403 forbidden"));
    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => {
      expect(host.textContent).toContain(
        "This role cannot read tenant Rightsizing opportunities. Metrics read permission is required.",
      );
      expect(host.textContent?.match(
        /This role cannot read tenant economics\. Metrics read permission is required\./g,
      )).toHaveLength(2);
    });
  });

  it("renders a failed-only estimated failure tax instead of an empty state", async () => {
    api.getRightsizingOpportunities.mockResolvedValue({
      total_cost_usd: 0,
      total_estimated_cost_usd: 0,
      nodes: [],
      note: "No spend.",
    });
    api.getUnitEconomics.mockResolvedValue({
      window_runs: 1,
      successful_runs: 0,
      failed_runs: 1,
      in_flight_runs: 0,
      success_rate: 0,
      total_cost_usd: 0,
      terminal_cost_usd: 0,
      cost_on_successful_usd: 0,
      cost_on_failed_usd: 0,
      cost_on_in_flight_usd: 0,
      cost_per_successful_run_usd: null,
      mean_cost_per_successful_run_usd: null,
      failure_tax_usd: 0,
      failure_tax_ratio: 0,
      estimated_total_cost_usd: 0.2,
      estimated_terminal_cost_usd: 0.2,
      estimated_cost_on_successful_usd: 0,
      estimated_cost_on_failed_usd: 0.2,
      estimated_cost_on_in_flight_usd: 0,
      estimated_cost_per_successful_run_usd: null,
      estimated_mean_cost_per_successful_run_usd: null,
      estimated_failure_tax_usd: 0.2,
      estimated_failure_tax_ratio: 1,
      runs_with_cost: 0,
      runs_with_estimated_cost: 1,
      by_workflow: [],
      by_tenant: [],
      quality: null,
      note: "No successful outcome yet; failure tax is estimated, not measured.",
    });
    api.getWaste.mockResolvedValue({
      window_runs: 1,
      runs_with_waste: 0,
      runs_with_cost: 0,
      total_cost_usd: 0,
      total_confirmed_waste_usd: 0,
      total_flagged_waste_usd: 0,
      waste_ratio: 0,
      findings: 0,
      confirmed_findings: 0,
      flagged_findings: 0,
      by_kind: [],
      top_findings: [],
      note: "No measured waste.",
    });

    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Failure tax");
      expect(host.textContent).toContain("$0.20");
      expect(host.textContent).toContain("estimated");
      expect(host.textContent).toContain("0 / 1 / 0");
    });
  });

  it("labels estimated-only node spend and projected savings", async () => {
    api.getRightsizingOpportunities.mockResolvedValue({
      total_cost_usd: 0,
      total_estimated_cost_usd: 0.4,
      nodes: [
        {
          node_id: "estimated-agent",
          runs: 2,
          total_cost_usd: 0,
          mean_cost_per_call_usd: 0,
          total_estimated_cost_usd: 0.4,
          mean_estimated_cost_per_call_usd: 0.2,
          incumbent_model: "gpt-4o",
          uses_tools: false,
          tool_free_runs: 2,
          cheaper_alternatives: 1,
          best_savings_pct: 50,
          projected_savings_usd: 0,
          projected_estimated_savings_usd: 0.2,
          experiment_ready: true,
        },
      ],
      note: "Spend is estimated, not measured provider dollars.",
    });
    api.getUnitEconomics.mockReturnValue(new Promise<never>(() => undefined));
    api.getWaste.mockReturnValue(new Promise<never>(() => undefined));

    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => {
      expect(host.textContent).toContain("estimated-agent");
      expect(host.textContent).toContain("$0.40 estimated");
      expect(host.textContent).toContain("≈ $0.20 estimated");
    });
  });

  it("constrains every opportunities column inside the card at desktop width", async () => {
    api.getRightsizingOpportunities.mockResolvedValue({
      total_cost_usd: 0.12,
      total_estimated_cost_usd: 0,
      nodes: [
        {
          node_id: "economics-agent",
          runs: 1,
          total_cost_usd: 0.12,
          mean_cost_per_call_usd: 0.12,
          total_estimated_cost_usd: 0,
          mean_estimated_cost_per_call_usd: 0,
          incumbent_model: "fixture/local-economics-model-v1",
          uses_tools: false,
          tool_free_runs: 1,
          cheaper_alternatives: 0,
          best_savings_pct: null,
          projected_savings_usd: null,
          projected_estimated_savings_usd: null,
          experiment_ready: false,
        },
      ],
      note: "No cheaper alternative.",
    });

    await act(async () => root.render(<RightsizingPage />));

    await vi.waitFor(() => {
      const table = host.querySelector<HTMLTableElement>(
        '[data-evidence-id="rightsizing.region.opportunities-scroll"] table',
      );
      expect(table?.style.tableLayout).toBe("fixed");
      expect(table?.querySelectorAll("col")).toHaveLength(8);
      expect(table?.querySelector("col:last-child")?.getAttribute("style"))
        .toContain("width: 9%");
      expect(table?.querySelector("tbody td:nth-child(2)")?.getAttribute("title"))
        .toBe("fixture/local-economics-model-v1");
      expect(table?.querySelector("tbody td:nth-child(4)")?.getAttribute("title"))
        .toBe("$0.12");
    });
  });
});
