<template>
  <main class="catalog-page">
    <header class="page-heading">
      <h1>共享 Agent 与 Skill</h1>
      <button type="button" :disabled="loading" @click="load(false)">刷新目录</button>
    </header>
    <PublicPersonalAgents />
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading && !items.length" role="status">正在加载共享目录…</p>
    <p v-else-if="loaded && !error && !items.length" class="empty">当前没有已批准的全局共享资产。</p>
    <section v-else-if="items.length" class="asset-grid" aria-label="全局共享 Agent 与 Skill">
      <article v-for="item in items" :key="item.id" class="asset-card">
        <header><div><h2>{{ item.title }}</h2><p>{{ kindLabel(item.kind) }} · v{{ item.version_number }} · 企业共享</p></div></header>
        <details><summary>查看配置</summary><p class="hash">版本摘要：{{ item.content_sha256 }}</p><pre>{{ JSON.stringify(item.definition, null, 2) }}</pre></details>
        <button v-if="item.xiaoling_runtime_config_compatible && item.personal_execution_available" type="button"
          :data-run-asset="item.id" @click="runner?.choose(item)">使用</button>
        <p v-else class="execution">{{ item.xiaoling_runtime_config_compatible ? '暂不可运行' : '暂不支持此版本' }}。</p>
      </article>
    </section>
    <button v-if="more" type="button" data-action="more-assets" :disabled="loading" @click="load(true)">加载更多</button>
    <GlobalEnterpriseAssetRunner ref="runner" :items="items" />
  </main>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { listGlobalEnterpriseAssetCatalog, type EnterpriseAssetCatalogItem } from '@/api/enterpriseAssets'
import { getToken } from '@/utils/token'
import PublicPersonalAgents from './PublicPersonalAgents.vue'
import GlobalEnterpriseAssetRunner from './GlobalEnterpriseAssetRunner.vue'
import { useUserStore } from '@/stores/user'

const user = useUserStore()
const items = ref<EnterpriseAssetCatalogItem[]>([])
const loading = ref(false)
const loaded = ref(false)
const more = ref(false)
const error = ref('')
const runner = ref<InstanceType<typeof GlobalEnterpriseAssetRunner> | null>(null)
let generation = 0

function kindLabel(kind: string) { return kind === 'agent' ? 'Agent' : 'Skill' }
async function load(append = false) {
  if (loading.value) return
  const request = ++generation, token = getToken()
  if (!token || token !== user.token || !user.profile?.id) return
  const before = append ? items.value.at(-1)?.id : undefined
  if (append && before === undefined) return
  loading.value = true; error.value = ''
  try {
    const rows = append ? await listGlobalEnterpriseAssetCatalog(token, before) : await listGlobalEnterpriseAssetCatalog(token)
    if (request !== generation || token !== getToken()) return
    items.value = append ? [...items.value, ...rows] : rows
    loaded.value = true
    more.value = rows.length === 100
  } catch {
    if (request !== generation || token !== getToken()) return
    error.value = '共享目录加载失败，请重试。'
  } finally { if (request === generation) loading.value = false }
}
watch(() => [user.profile?.id, user.token] as const, () => {
  ++generation; items.value = []; error.value = ''; loading.value = false; loaded.value = false; more.value = false; void load()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { ++generation })
</script>

<style scoped>
.catalog-page { max-width: 1120px; margin: 0 auto; padding: 28px; }
.page-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:24px; }
h1 { margin:0 0 6px; font-size:24px; }.page-heading p,.asset-card p { color:#667085; }
button { border:1px solid #d5dae5; border-radius:9px; padding:9px 13px; background:#fff; cursor:pointer; }
button:disabled { opacity:.6; cursor:default; }.asset-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }
.asset-card { padding:18px; border:1px solid #e4e7ef; border-radius:14px; background:#fff; }
.asset-card header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
h2 { margin:0 0 5px; font-size:17px; }
.hash { overflow-wrap:anywhere; font:12px/1.6 ui-monospace,monospace; }.asset-card details { margin-top:12px; }
.asset-card summary { cursor:pointer; color:#334155; }.asset-card pre { max-height:360px; overflow:auto; padding:12px; border-radius:8px; background:#f6f7fa; white-space:pre-wrap; overflow-wrap:anywhere; }
.execution { margin-bottom:0; font-size:13px; }.empty { padding:28px; color:#667085; text-align:center; }.error { color:#b42318; }
@media(max-width:640px) { .catalog-page { padding:18px; }.page-heading { align-items:flex-start; flex-direction:column; } }
</style>
