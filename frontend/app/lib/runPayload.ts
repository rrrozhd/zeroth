type JsonSchema = Record<string, unknown>;

function record(value: unknown): JsonSchema | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonSchema)
    : null;
}

function resolveLocalRef(root: JsonSchema, ref: string): JsonSchema | null {
  if (!ref.startsWith("#/")) return null;
  let current: unknown = root;
  for (const rawPart of ref.slice(2).split("/")) {
    const part = rawPart.replaceAll("~1", "/").replaceAll("~0", "~");
    const currentRecord = record(current);
    if (currentRecord === null || !(part in currentRecord)) return null;
    current = currentRecord[part];
  }
  return record(current);
}

function stringExample(schema: JsonSchema): string {
  let value = "example";
  if (typeof schema.pattern === "string") {
    const exactLiteral = schema.pattern.match(/^\^([A-Za-z0-9_-]+)\$$/)?.[1];
    if (exactLiteral) value = exactLiteral;
    // Common identifier contracts use an anchored literal prefix followed by a
    // broader character class (for example, ^synthetic-[a-z0-9-]+$). Preserve
    // that prefix so the generated cURL is immediately runnable.
    const literalPrefix = schema.pattern.match(/^\^([A-Za-z0-9_-]+)/)?.[1];
    if (!exactLiteral && literalPrefix) value = `${literalPrefix}example`;
  }
  if (typeof schema.minLength === "number" && value.length < schema.minLength) {
    value = value.padEnd(schema.minLength, "x");
  }
  if (typeof schema.maxLength === "number" && value.length > schema.maxLength) {
    value = value.slice(0, schema.maxLength);
  }
  return value;
}

function numberExample(schema: JsonSchema): number {
  const multiple = typeof schema.multipleOf === "number" && schema.multipleOf > 0
    ? schema.multipleOf
    : null;
  const exclusive = typeof schema.exclusiveMinimum === "number"
    ? schema.exclusiveMinimum
    : null;
  const lower = exclusive ?? (typeof schema.minimum === "number" ? schema.minimum : 0);
  let value = multiple === null ? lower : Math.ceil(lower / multiple) * multiple;
  if (exclusive !== null && value <= exclusive) value += multiple ?? 1;
  if (schema.type === "integer" && !Number.isInteger(value)) value = Math.ceil(value);
  return value;
}

function example(schema: JsonSchema, root: JsonSchema): unknown {
  if (schema.const !== undefined) return schema.const;
  if (schema.default !== undefined) return schema.default;
  if (Array.isArray(schema.examples) && schema.examples.length > 0) return schema.examples[0];
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return schema.enum[0];

  if (typeof schema.$ref === "string") {
    const resolved = resolveLocalRef(root, schema.$ref);
    if (resolved !== null) return example(resolved, root);
  }
  for (const key of ["oneOf", "anyOf"] as const) {
    if (!Array.isArray(schema[key])) continue;
    const candidate = schema[key]
      .map(record)
      .find((item): item is JsonSchema => item !== null && item.type !== "null");
    if (candidate) return example(candidate, root);
  }

  const type = schema.type;
  if (type === "object" || record(schema.properties) !== null) {
    const properties = record(schema.properties) ?? {};
    const required = new Set(
      Array.isArray(schema.required)
        ? schema.required.filter((item): item is string => typeof item === "string")
        : [],
    );
    return Object.fromEntries(
      Object.entries(properties)
        .filter(([name]) => required.has(name))
        .map(([name, child]) => [name, example(record(child) ?? {}, root)]),
    );
  }
  if (type === "array") {
    const itemSchema = record(schema.items) ?? {};
    const length = Math.max(1, typeof schema.minItems === "number" ? schema.minItems : 1);
    return Array.from({ length }, () => example(itemSchema, root));
  }
  if (type === "integer" || type === "number") {
    return numberExample(schema);
  }
  if (type === "boolean") return false;
  if (type === "null") return null;
  return stringExample(schema);
}

/** Build an editable best-effort payload for common deployment JSON Schema constraints. */
export function examplePayloadFromSchema(schema: unknown): unknown {
  const root = record(schema) ?? {};
  return example(root, root);
}
