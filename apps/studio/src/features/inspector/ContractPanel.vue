<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { createValidationApiClient } from "@/lib/api/validation";

const props = defineProps<{
  inputContractRef: string | null;
  outputContractRef: string | null;
}>();

const validationClient = createValidationApiClient();

const contractRefs = computed(() =>
  [
    { key: "input", label: "Input contract", ref: props.inputContractRef },
    { key: "output", label: "Output contract", ref: props.outputContractRef },
  ].filter((entry): entry is { key: string; label: string; ref: string } => Boolean(entry.ref)),
);

const contractQueries = useQuery({
  queryKey: computed(() => ["studio", "contracts", contractRefs.value.map((entry) => entry.ref)]),
  queryFn: async () => {
    const results = await Promise.all(
      contractRefs.value.map(async (entry) => ({
        ...entry,
        schema: await validationClient.lookupContract(entry.ref),
      })),
    );
    return results;
  },
  enabled: computed(() => contractRefs.value.length > 0),
});

const contractResults = computed(() => contractQueries.data.value ?? []);
</script>

<template>
  <section class="contract-panel">
    <div class="node-inspector__eyebrow">
      <span>Contract summary</span>
    </div>

    <div v-if="contractRefs.length === 0" class="node-inspector__card">
      <strong>No contracts linked</strong>
      <p class="studio-body">Attach input or output contracts from this node to keep schema checks local.</p>
    </div>

    <div v-for="contract in contractResults" :key="contract.key" class="node-inspector__card">
      <strong>{{ contract.label }}</strong>
      <p class="studio-body">{{ contract.ref }}</p>
      <p class="studio-body">
        {{ String(contract.schema.json_schema.title ?? contract.schema.name) }}
        <span v-if="contract.schema.version">· v{{ contract.schema.version }}</span>
      </p>
    </div>
  </section>
</template>
