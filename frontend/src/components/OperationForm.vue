<script setup lang="ts">
import { reactive } from 'vue'
import type { FormSpec } from '../ui'
const props = defineProps<{spec: FormSpec; busy: boolean}>()
const emit = defineEmits<{ submit: [values: Record<string,string>]; cancel: [] }>()
const values = reactive<Record<string,string>>(Object.fromEntries(props.spec.fields.map(f => [f.key, f.initial ?? ''])))
</script>
<template>
  <section class="operation" aria-label="业务表单">
    <h2>{{ spec.title }}</h2>
    <form @submit.prevent="emit('submit', {...values})">
      <div class="fields">
        <label v-for="field in spec.fields" :key="field.key">
          <span>{{ field.label }}</span>
          <select v-if="field.type === 'select'" v-model="values[field.key]" :required="field.required !== false" :disabled="busy">
            <option value="">请选择</option>
            <option v-for="o in field.options" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <input v-else v-model="values[field.key]" :type="field.type ?? 'text'" :required="field.required !== false" :disabled="busy" :step="field.type === 'datetime-local' ? '1' : undefined" />
          <small v-if="field.hint">{{ field.hint }}</small>
        </label>
      </div>
      <p v-if="spec.confirm" class="notice">{{ spec.confirm }}</p>
      <button type="submit" :disabled="busy">确认提交</button>
      <button type="button" :disabled="busy" @click="emit('cancel')">取消</button>
    </form>
  </section>
</template>
