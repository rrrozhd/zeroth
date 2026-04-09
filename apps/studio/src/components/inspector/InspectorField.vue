<script setup lang="ts">
import type { PropertyDefinition } from '../../types/nodes'

defineProps<{
  definition: PropertyDefinition
  modelValue: unknown
}>()

const emit = defineEmits<{
  'update:modelValue': [value: unknown]
}>()
</script>

<template>
  <div class="inspector-field">
    <label class="inspector-field__label">
      {{ definition.label }}
      <span v-if="definition.required" class="inspector-field__required">*</span>
    </label>

    <input
      v-if="definition.type === 'text'"
      type="text"
      :value="modelValue"
      :placeholder="definition.placeholder"
      :required="definition.required"
      class="inspector-field__input"
      @change="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />

    <textarea
      v-else-if="definition.type === 'textarea'"
      :value="modelValue as string"
      :placeholder="definition.placeholder"
      rows="3"
      class="inspector-field__textarea"
      @change="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />

    <input
      v-else-if="definition.type === 'number'"
      type="number"
      :value="modelValue"
      class="inspector-field__input"
      @change="emit('update:modelValue', Number(($event.target as HTMLInputElement).value))"
    />

    <select
      v-else-if="definition.type === 'select'"
      :value="modelValue"
      class="inspector-field__select"
      @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
    >
      <option v-for="opt in definition.options" :key="opt.value" :value="opt.value">
        {{ opt.label }}
      </option>
    </select>

    <label v-else-if="definition.type === 'toggle'" class="inspector-field__toggle">
      <input
        type="checkbox"
        :checked="!!modelValue"
        @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)"
      />
      <span>{{ modelValue ? 'Enabled' : 'Disabled' }}</span>
    </label>
  </div>
</template>

<style scoped>
.inspector-field {
  display: flex;
  flex-direction: column;
}

.inspector-field__label {
  font-size: 12px;
  color: var(--color-studio-text-secondary, #5a7a8a);
  margin-bottom: 4px;
  font-weight: 500;
}

.inspector-field__required {
  color: rgba(255, 80, 80, 0.8);
  margin-left: 2px;
}

.inspector-field__input,
.inspector-field__textarea,
.inspector-field__select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid rgba(118, 182, 205, 0.3);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.5);
  font-size: 13px;
  font-family: inherit;
  color: #123044;
  transition: border-color 150ms ease;
  box-sizing: border-box;
}

.inspector-field__input:focus,
.inspector-field__textarea:focus,
.inspector-field__select:focus {
  border-color: rgba(79, 205, 255, 0.6);
  outline: none;
}

.inspector-field__textarea {
  resize: vertical;
}

.inspector-field__toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #123044;
  cursor: pointer;
}

.inspector-field__toggle input[type="checkbox"] {
  width: 16px;
  height: 16px;
  cursor: pointer;
}
</style>
