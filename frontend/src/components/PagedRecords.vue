<script setup lang="ts">
import {ref, watch, onBeforeUnmount} from 'vue'
import {api, ApiError, page, type Page} from '../api/client'
import RecordTable from './RecordTable.vue'
const props = defineProps<{path:string; columns:{key:string;label:string}[]; refresh:number; accountId?:string}>()
const emit=defineEmits<{unauthorized:[error:ApiError]}>()
const result = ref<Page|null>(null), error = ref(''), loading = ref(false)
let serial = 0
async function load(offset = 0, revision?:number) {
  const token = ++serial, path = props.path, account = props.accountId
  loading.value = true; error.value = ''
  try {
    const r = page(await api(`${path}${path.includes('?')?'&':'?'}limit=50&offset=${offset}${revision === undefined?'':`&ledger_revision=${revision}`}`))
    if (token === serial && props.path === path && props.accountId === account) {
      if(account&&r.items.some(row=>row.account_id!==account))throw new Error('列表返回其他账户的记录，已拒绝展示')
      result.value = r
    }
  } catch (e) { if(token===serial) {error.value=String(e);result.value=null;if(e instanceof ApiError&&e.status===401)emit('unauthorized',e)} }
  finally {if(token===serial) loading.value=false}
}
watch(() => [props.path,props.refresh,props.accountId], () => {result.value=null;void load()}, {immediate:true,flush:'sync'})
onBeforeUnmount(() => {serial++})
</script>
<template>
  <p v-if="loading">正在读取…</p><p v-if="error" role="alert">{{ error }} <button @click="load()">重新读取</button></p>
  <template v-if="result">
    <RecordTable :rows="result.items" :columns="columns">
      <template v-if="$slots.actions" #actions="{row}"><slot name="actions" :row="row" /></template>
    </RecordTable>
    <p>共 {{result.total}} 条；账本版本 {{result.ledger_revision}}
      <button :disabled="loading || result.offset===0" @click="load(Math.max(0,result.offset-result.limit),result.ledger_revision)">上一页</button>
      <button :disabled="loading || result.offset+result.limit>=result.total" @click="load(result.offset+result.limit,result.ledger_revision)">下一页</button>
    </p>
  </template>
</template>
