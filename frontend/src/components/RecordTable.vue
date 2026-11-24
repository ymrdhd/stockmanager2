<script setup lang="ts">
import { display } from '../ui'
import type { Fact } from '../api/client'
defineProps<{rows: Fact[]; columns: {key:string; label:string}[]}>()
</script>
<template>
  <p v-if="!rows.length">暂无记录。</p>
  <div v-else class="table-scroll">
    <table><thead><tr><th v-for="column in columns" :key="column.key" scope="col">{{ column.label }}</th><th v-if="$slots.actions" scope="col">操作</th></tr></thead>
      <tbody><tr v-for="(row,index) in rows" :key="String(row.id ?? row.lot_id ?? row.instrument_id ?? index)">
        <td v-for="column in columns" :key="column.key">{{ display(row[column.key],column.key) }}</td>
        <td v-if="$slots.actions"><slot name="actions" :row="row" /></td>
      </tr></tbody>
    </table>
  </div>
</template>
