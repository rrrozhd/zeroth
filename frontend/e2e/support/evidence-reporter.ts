import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import type {
  FullConfig,
  FullResult,
  Reporter,
  TestCase,
  TestResult,
} from "@playwright/test/reporter";
import { containsSecretShape } from "./secret-shapes";

export type IndexedArtifact = { source: string; destination: string };
export type IndexedTestResult = {
  testId: string;
  title: string;
  status: "passed" | "failed" | "skipped";
  criteria: string[];
  artifacts: IndexedArtifact[];
};

type EvidenceIndex = {
  schema_version: 1;
  completed: boolean;
  criteria: Array<{
    criterion_id: string;
    status: "pass" | "fail";
    test_id: string;
    evidence: string[];
  }>;
  artifacts: IndexedArtifact[];
};

export function buildEvidenceIndex(
  results: IndexedTestResult[],
  completed: boolean,
): EvidenceIndex {
  const artifacts = new Map<string, IndexedArtifact>();
  for (const result of results) {
    for (const artifact of result.artifacts) artifacts.set(artifact.destination, artifact);
  }
  const html: IndexedArtifact = {
    source: "html-report/index.html",
    destination: "playwright-report/index.html",
  };
  artifacts.set(html.destination, html);

  const criteria = new Map<string, IndexedTestResult[]>();
  for (const result of results) {
    if (result.status === "skipped") continue;
    for (const criterion of result.criteria) {
      const rows = criteria.get(criterion) ?? [];
      rows.push(result);
      criteria.set(criterion, rows);
    }
  }
  return {
    schema_version: 1,
    completed,
    criteria: [...criteria.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([criterion, rows]) => ({
        criterion_id: criterion,
        status: rows.every((row) => row.status === "passed") ? "pass" : "fail",
        test_id: rows.map((row) => row.testId).sort().join(","),
        evidence: [...new Set(rows.flatMap((row) => row.artifacts.map((item) => item.destination)))]
          .concat(rows.some((row) => row.artifacts.length > 0) ? [] : [html.destination])
          .sort(),
      })),
    artifacts: [...artifacts.values()].sort((left, right) =>
      left.destination.localeCompare(right.destination)),
  };
}

function category(name: string, contentType: string): string {
  if (contentType === "image/png") return "screenshots";
  if (contentType === "video/webm") return "videos";
  if (name.includes("axe")) return "accessibility";
  if (name.includes("network")) return "network";
  return "console";
}

function extension(contentType: string): string {
  return {
    "application/json": ".json",
    "image/png": ".png",
    "text/html": ".html",
    "video/webm": ".webm",
  }[contentType] ?? ".json";
}

export function artifactFilename(
  projectName: string,
  testId: string,
  attachmentIndex: number,
  attachmentName: string,
  contentType: string,
): string {
  const project = projectName.replace(/[^a-z0-9_-]/gi, "-");
  const digest = createHash("sha256")
    .update(`${testId}:${attachmentIndex}:${attachmentName}`)
    .digest("hex")
    .slice(0, 16);
  const name = attachmentName.replace(/[^a-z0-9_-]/gi, "-");
  return `${project}-${digest}-${name}${extension(contentType)}`;
}

export default class EvidenceReporter implements Reporter {
  private root = "";
  private readonly results: IndexedTestResult[] = [];

  onBegin(_config: FullConfig): void {
    this.root = path.resolve(
      process.env.ZEROTH_EVALUATION_BROWSER_ROOT ?? "output/playwright",
    );
    mkdirSync(path.join(this.root, "indexed"), { recursive: true });
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const projectName = test.parent.project()?.name ?? "unknown-project";
    const criteria = test.annotations
      .filter((annotation) => annotation.type === "criterion" && annotation.description)
      .map((annotation) => annotation.description!);
    const artifacts: IndexedArtifact[] = [];
    for (const [index, attachment] of result.attachments.entries()) {
      const filename = artifactFilename(
        projectName,
        test.id,
        index,
        attachment.name,
        attachment.contentType,
      );
      const source = `indexed/${filename}`;
      const body = attachment.body ?? (attachment.path ? readFileSync(attachment.path) : null);
      if (!body) continue;
      if (attachment.contentType !== "image/png" && attachment.contentType !== "video/webm") {
        const text = body.toString("utf-8");
        if (containsSecretShape(text)) throw new Error("secret-shaped Playwright attachment rejected");
      }
      writeFileSync(path.join(this.root, source), body, { flag: "wx" });
      artifacts.push({
        source,
        destination: `${category(attachment.name, attachment.contentType)}/${filename}`,
      });
    }
    this.results.push({
      testId: test.id,
      title: test.title,
      status: result.status === "passed" ? "passed" : result.status === "skipped" ? "skipped" : "failed",
      criteria,
      artifacts,
    });
  }

  onEnd(result: FullResult): void {
    const index = buildEvidenceIndex(this.results, result.status !== "interrupted");
    const encoded = JSON.stringify(index, null, 2) + "\n";
    if (containsSecretShape(encoded)) throw new Error("secret-shaped Playwright index rejected");
    writeFileSync(path.join(this.root, "results.json"), encoded, { flag: "wx" });
  }
}
