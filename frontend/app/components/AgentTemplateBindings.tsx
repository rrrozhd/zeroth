"use client";

import { ConsoleField, ConsoleNotice, consoleControlClassName } from "@/app/components/primitives";
import type { Template } from "@/app/lib/api";

type TemplateReference = {
  name: string;
  version?: number | null;
};

type TemplateMemoryBinding = {
  as_name: string;
  connector_instance_id: string;
  access_mode: "get" | "scan";
  key?: string;
  key_prefix?: string;
  default?: unknown;
  max_items?: number;
  scope: "run" | "thread" | "shared";
};

type AgentTemplateConfig = Record<string, unknown> & {
  template_ref?: TemplateReference;
  memory_refs?: string[];
  template_memory_bindings?: TemplateMemoryBinding[];
};

function forbidden(message: string | null | undefined): boolean {
  return Boolean(message && /(?:^|:\s*)403(?:\s|$)/.test(message.trim()));
}

function templateNames(templates: Template[], selected: string): string[] {
  return Array.from(new Set([selected, ...templates.map((template) => template.name)].filter(Boolean)))
    .sort((left, right) => left.localeCompare(right));
}

function templateVersions(templates: Template[], name: string, selected?: number | null): number[] {
  return Array.from(
    new Set([
      ...(selected === undefined || selected === null ? [] : [selected]),
      ...templates.filter((template) => template.name === name).map((template) => template.version),
    ]),
  ).sort((left, right) => right - left);
}

export function AgentTemplateBindings({
  config,
  templates,
  connectors,
  templateAccessError,
  readOnly,
  onChange,
}: {
  config: AgentTemplateConfig;
  templates: Template[];
  connectors: string[];
  templateAccessError?: string | null;
  readOnly: boolean;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const ref = config.template_ref;
  const selectedName = typeof ref?.name === "string" ? ref.name : "";
  const selectedVersion = typeof ref?.version === "number" ? ref.version : null;
  const bindings = Array.isArray(config.template_memory_bindings)
    ? config.template_memory_bindings
    : [];
  const accessUnavailable = Boolean(templateAccessError);
  const controlDisabled = readOnly || accessUnavailable;
  const names = templateNames(templates, selectedName);
  const versions = templateVersions(templates, selectedName, selectedVersion);
  const selectedTemplate = templates.find(
    (template) =>
      template.name === selectedName &&
      (selectedVersion === null || template.version === selectedVersion),
  );

  function emit(patch: Partial<AgentTemplateConfig>, remove: (keyof AgentTemplateConfig)[] = []) {
    const next: AgentTemplateConfig = { ...config, ...patch };
    for (const key of remove) delete next[key];
    onChange(next);
  }

  function setTemplateName(name: string) {
    if (!name) {
      emit({}, ["template_ref", "template_memory_bindings"]);
      return;
    }
    emit({ template_ref: { name } });
  }

  function setTemplateVersion(raw: string) {
    if (!selectedName) return;
    emit({
      template_ref: raw === "latest"
        ? { name: selectedName }
        : { name: selectedName, version: Number(raw) },
    });
  }

  function addBinding() {
    const connector = connectors[0];
    if (!connector) return;
    const nextBinding: TemplateMemoryBinding = {
      as_name: "",
      connector_instance_id: connector,
      access_mode: "get",
      key: "",
      scope: "run",
    };
    emit({
      memory_refs: Array.from(new Set([...(config.memory_refs ?? []), connector])),
      template_memory_bindings: [...bindings, nextBinding],
    });
  }

  function patchBinding(index: number, patch: Partial<TemplateMemoryBinding>) {
    let nextBinding = { ...bindings[index], ...patch };
    if (patch.access_mode === "get") {
      const { key_prefix: _prefix, max_items: _max, ...getBinding } = nextBinding;
      nextBinding = getBinding;
    }
    if (patch.access_mode === "scan") {
      const { key: _key, ...scanBinding } = nextBinding;
      nextBinding = scanBinding;
    }
    const nextBindings = bindings.map((binding, bindingIndex) =>
      bindingIndex === index ? nextBinding : binding,
    );
    const connector = nextBinding.connector_instance_id;
    emit({
      memory_refs: connector
        ? Array.from(new Set([...(config.memory_refs ?? []), connector]))
        : config.memory_refs ?? [],
      template_memory_bindings: nextBindings,
    });
  }

  function removeBinding(index: number) {
    emit({ template_memory_bindings: bindings.filter((_, bindingIndex) => bindingIndex !== index) });
  }

  return (
    <fieldset
      data-evidence-id="studio.agent.template"
      className="space-y-4 border-t border-border pt-4"
    >
      <legend className="text-sm font-medium">Prompt template</legend>
      <p className="text-xs leading-relaxed text-muted">
        Resolve a tenant-scoped, immutable prompt template at run time. Pin a version for
        reproducibility or follow the latest registered version.
      </p>

      {templateAccessError && (
        <div data-evidence-id="studio.agent.template.access">
          <ConsoleNotice>
            {forbidden(templateAccessError)
              ? "Template library access is restricted for this role. Saved references remain visible, but cannot be changed."
              : `Template library is unavailable. Saved references remain visible, but cannot be changed. ${templateAccessError}`}
          </ConsoleNotice>
        </div>
      )}

      {accessUnavailable && selectedName ? (
        <>
          <ConsoleField label="Template name">
            <input
              value={selectedName}
              disabled
              readOnly
              data-evidence-id="studio.agent.template.name"
              className={consoleControlClassName}
            />
          </ConsoleField>
          <ConsoleField label="Template version">
            <input
              value={selectedVersion ?? "Latest"}
              disabled
              readOnly
              data-evidence-id="studio.agent.template.version"
              className={consoleControlClassName}
            />
          </ConsoleField>
        </>
      ) : !accessUnavailable ? (
        <>
          <ConsoleField
            label="Template name"
            hint={names.length === 0 ? "Register a template from Build → Templates first." : undefined}
          >
            <select
              value={selectedName}
              disabled={readOnly || names.length === 0}
              onChange={(event) => setTemplateName(event.target.value)}
              data-evidence-id="studio.agent.template.name"
              className={consoleControlClassName}
            >
              <option value="">Use inline instruction</option>
              {names.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </ConsoleField>

          {selectedName && (
            <ConsoleField
              label="Template version"
              hint={selectedVersion === null
                ? "Latest registered version is resolved when the run starts."
                : `Pinned to version ${selectedVersion}.`}
            >
              <select
                value={selectedVersion === null ? "latest" : String(selectedVersion)}
                disabled={readOnly}
                onChange={(event) => setTemplateVersion(event.target.value)}
                data-evidence-id="studio.agent.template.version"
                className={consoleControlClassName}
              >
                <option value="latest">Latest</option>
                {versions.map((version) => (
                  <option key={version} value={version}>Version {version}</option>
                ))}
              </select>
            </ConsoleField>
          )}
        </>
      ) : null}

      {selectedName && selectedTemplate?.variables.length ? (
        <p className="text-xs text-muted">
          Variables: {selectedTemplate.variables.join(", ")}
        </p>
      ) : null}

      {selectedName && (
        <div className="space-y-3" data-evidence-id="studio.agent.template.memory">
          <div>
            <span className="block text-sm font-medium">Template memory bindings</span>
            <span className="mt-0.5 block text-xs leading-relaxed text-muted">
              Read a connector value into the template&apos;s <code>memory</code> namespace.
              Selected connectors are also added to this agent&apos;s memory references.
            </span>
          </div>

          {bindings.length === 0 && (
            <p className="rounded-lg border border-border bg-raised px-3 py-2 text-xs text-muted">
              No template memory is bound.
            </p>
          )}

          {bindings.map((binding, index) => {
            const connectorOptions = Array.from(
              new Set([binding.connector_instance_id, ...connectors].filter(Boolean)),
            );
            return (
              <div
                key={index}
                className="space-y-3 rounded-lg border border-border bg-raised p-3"
                data-evidence-id={`studio.agent.template.memory.${index}`}
              >
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs font-semibold">Binding {index + 1}</span>
                  {!controlDisabled && (
                    <button
                      type="button"
                      aria-label={`Remove memory binding ${index + 1}`}
                      data-evidence-id={`studio.agent.template.memory.${index}.remove`}
                      onClick={() => removeBinding(index)}
                      className="text-xs font-medium text-muted hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                    >
                      Remove
                    </button>
                  )}
                </div>

                <ConsoleField label="Memory name" hint={`Available as memory.${binding.as_name || "name"}.`} required>
                  <input
                    value={binding.as_name}
                    disabled={controlDisabled}
                    required
                    onChange={(event) => patchBinding(index, { as_name: event.target.value })}
                    data-evidence-id={`studio.agent.template.memory.${index}.alias`}
                    className={consoleControlClassName}
                  />
                </ConsoleField>

                <ConsoleField label="Connector" required>
                  <select
                    value={binding.connector_instance_id}
                    disabled={controlDisabled}
                    required
                    onChange={(event) => patchBinding(index, { connector_instance_id: event.target.value })}
                    data-evidence-id={`studio.agent.template.memory.${index}.connector`}
                    className={consoleControlClassName}
                  >
                    {connectorOptions.map((connector) => (
                      <option key={connector} value={connector}>{connector}</option>
                    ))}
                  </select>
                </ConsoleField>

                <ConsoleField label="Access mode">
                  <select
                    value={binding.access_mode}
                    disabled={controlDisabled}
                    onChange={(event) => patchBinding(index, {
                      access_mode: event.target.value as TemplateMemoryBinding["access_mode"],
                    })}
                    data-evidence-id={`studio.agent.template.memory.${index}.access-mode`}
                    className={consoleControlClassName}
                  >
                    <option value="get">Get one value</option>
                    <option value="scan">Scan a prefix</option>
                  </select>
                </ConsoleField>

                {binding.access_mode === "get" ? (
                  <ConsoleField label="Key" required>
                    <input
                      value={binding.key ?? ""}
                      disabled={controlDisabled}
                      required
                      onChange={(event) => patchBinding(index, { key: event.target.value })}
                      data-evidence-id={`studio.agent.template.memory.${index}.key`}
                      className={consoleControlClassName}
                    />
                  </ConsoleField>
                ) : (
                  <>
                    <ConsoleField label="Key prefix">
                      <input
                        value={binding.key_prefix ?? ""}
                        disabled={controlDisabled}
                        onChange={(event) => patchBinding(index, { key_prefix: event.target.value })}
                        data-evidence-id={`studio.agent.template.memory.${index}.key-prefix`}
                        className={consoleControlClassName}
                      />
                    </ConsoleField>
                    <ConsoleField label="Maximum items" hint="Blank uses the connector default.">
                      <input
                        type="number"
                        min={1}
                        value={binding.max_items ?? ""}
                        disabled={controlDisabled}
                        onChange={(event) => {
                          const raw = event.target.value;
                          const next = { ...binding };
                          if (raw === "") delete next.max_items;
                          else next.max_items = Number(raw);
                          patchBinding(index, next);
                        }}
                        data-evidence-id={`studio.agent.template.memory.${index}.max-items`}
                        className={consoleControlClassName}
                      />
                    </ConsoleField>
                  </>
                )}

                <ConsoleField label="Scope">
                  <select
                    value={binding.scope}
                    disabled={controlDisabled}
                    onChange={(event) => patchBinding(index, {
                      scope: event.target.value as TemplateMemoryBinding["scope"],
                    })}
                    data-evidence-id={`studio.agent.template.memory.${index}.scope`}
                    className={consoleControlClassName}
                  >
                    <option value="run">Run</option>
                    <option value="thread">Thread</option>
                    <option value="shared">Shared</option>
                  </select>
                </ConsoleField>

                <ConsoleField label="Default value" hint="Optional fallback when memory has no match.">
                  <input
                    value={binding.default === undefined || binding.default === null
                      ? ""
                      : String(binding.default)}
                    disabled={controlDisabled}
                    onChange={(event) => {
                      const next = { ...binding };
                      if (event.target.value === "") delete next.default;
                      else next.default = event.target.value;
                      patchBinding(index, next);
                    }}
                    data-evidence-id={`studio.agent.template.memory.${index}.default`}
                    className={consoleControlClassName}
                  />
                </ConsoleField>
              </div>
            );
          })}

          {!controlDisabled && (
            <button
              type="button"
              disabled={connectors.length === 0}
              onClick={addBinding}
              data-evidence-id="studio.agent.template.memory.add"
              className="text-xs font-medium text-accent hover:underline disabled:cursor-not-allowed disabled:text-muted disabled:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Add memory binding
            </button>
          )}
          {!controlDisabled && connectors.length === 0 && (
            <p className="text-xs text-muted">Register a memory connector before adding a binding.</p>
          )}
        </div>
      )}
    </fieldset>
  );
}
