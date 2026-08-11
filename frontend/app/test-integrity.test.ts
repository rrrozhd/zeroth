import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import vitestConfig from "../vitest.config";

function testFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(dir, entry.name);
    if (entry.isDirectory()) return testFiles(path);
    return /\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function findVacuity(source: string, name: string): string[] {
  const file = ts.createSourceFile(
    name,
    source,
    ts.ScriptTarget.Latest,
    true,
    name.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const problems: string[] = [];
  const line = (node: ts.Node) => file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
  const text = (node: ts.Node) => node.getText(file).replace(/\s+/g, " ");
  const hasCall = (node: ts.Node) => {
    let found = false;
    const visit = (child: ts.Node) => {
      if (ts.isCallExpression(child)) found = true;
      else ts.forEachChild(child, visit);
    };
    visit(node);
    return found;
  };

  function assertion(statement: ts.Statement): string | null {
    if (!ts.isExpressionStatement(statement) || !text(statement).startsWith("expect(")) return null;
    let calls = 0;
    const count = (node: ts.Node) => {
      if (ts.isCallExpression(node)) calls += 1;
      ts.forEachChild(node, count);
    };
    count(statement);
    return calls === 2 ? text(statement) : null;
  }

  function visit(node: ts.Node): void {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "each" &&
      ts.isArrayLiteralExpression(node.arguments[0]) &&
      node.arguments[0].elements.length === 0
    ) {
      problems.push(`${name}:${line(node)} parametrizes zero cases`);
    }

    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ["toBe", "toEqual", "toStrictEqual"].includes(node.expression.name.text) &&
      ts.isCallExpression(node.expression.expression) &&
      ts.isIdentifier(node.expression.expression.expression) &&
      node.expression.expression.expression.text === "expect" &&
      node.expression.expression.arguments.length === 1 &&
      node.arguments.length === 1 &&
      !hasCall(node.expression.expression.arguments[0]) &&
      !hasCall(node.arguments[0]) &&
      text(node.expression.expression.arguments[0]) === text(node.arguments[0])
    ) {
      problems.push(`${name}:${line(node)} compares a value with itself`);
    }

    const statements = ts.isSourceFile(node) || ts.isBlock(node) ? node.statements : [];
    for (let index = 1; index < statements.length; index += 1) {
      const previous = assertion(statements[index - 1]);
      const current = assertion(statements[index]);
      if (current !== null && current === previous) {
        problems.push(`${name}:${line(statements[index])} repeats the assertion above it`);
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(file);
  return problems;
}

describe("TypeScript test integrity", () => {
  it("keeps TSX component tests collected", () => {
    expect(vitestConfig.test?.include).toContain("app/**/*.test.{ts,tsx}");
  });

  it("detects each named vacuity shape", () => {
    const problems = findVacuity(
      `
        test.each([])("empty", () => {});
        it("self", () => { const value = 1; expect(value).toBe(value); });
        it("duplicate", () => {
          expect(value).toBe(true);
          expect(value).toBe(true);
        });
      `,
      "intentional-offender.test.ts",
    );

    expect(problems).toHaveLength(3);
    expect(problems.some((problem) => problem.includes("parametrizes zero cases"))).toBe(true);
    expect(problems.some((problem) => problem.includes("compares a value with itself"))).toBe(true);
    expect(problems.some((problem) => problem.includes("repeats the assertion"))).toBe(true);
  });

  it("keeps tracked TypeScript tests non-vacuous", () => {
    const problems = testFiles(import.meta.dirname).flatMap((path) =>
      findVacuity(readFileSync(path, "utf8"), path),
    );

    expect(problems, problems.join("\n")).toEqual([]);
  });
});
