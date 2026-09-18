<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'

import { getSecurityRuleCatalog } from '@/api/security'
import { actionableError, mustDiscardReadSnapshot } from '@/composables/withFeedback'
import type { SecurityRuleCatalogOut } from '@/types/security'

const loading = ref(false)
const catalog = ref<SecurityRuleCatalogOut | null>(null)
const loadError = ref('')
const keyword = ref('')
const origin = ref('')
let disposed = false
let requestVersion = 0

const originLabels: Record<string, string> = {
  review_rule: '审查提示规则',
  platform_static: '平台静态规则',
  platform_secret: '敏感信息规则',
}
const executorLabels: Record<string, string> = {
  deepseek_prompt: '大模型提示审查',
  static_analyzer: '确定性静态分析',
  secret_scanner: '敏感信息扫描',
}
const engineStatusLabels: Record<string, string> = {
  ready: '执行器就绪',
  ci_configured: 'CI 已配置',
  cli_detected: 'CLI 已检测',
  not_configured: '未配置',
  failed: '检查失败',
}

const filteredItems = computed(() => {
  const query = keyword.value.trim().toLocaleLowerCase()
  return (catalog.value?.items ?? []).filter((item) => {
    if (origin.value && item.origin !== origin.value) return false
    if (!query) return true
    return [item.code, item.name, item.description, item.cwe, item.owasp, item.language]
      .join(' ')
      .toLocaleLowerCase()
      .includes(query)
  })
})

async function loadCatalog(): Promise<void> {
  if (loading.value) return
  const version = ++requestVersion
  loading.value = true
  try {
    const result = await getSecurityRuleCatalog()
    if (disposed || version !== requestVersion) return
    catalog.value = result
    loadError.value = ''
  } catch (error) {
    if (disposed || version !== requestVersion) return
    if (mustDiscardReadSnapshot(error)) catalog.value = null
    const detail = actionableError(error, '统一规则目录读取失败')
    loadError.value = `${detail.message}。${detail.nextAction || '请稍后重试。'}${detail.requestId ? ` 请求编号：${detail.requestId}` : ''}`
  } finally {
    if (!disposed && version === requestVersion) loading.value = false
  }
}

function originLabel(value: string): string {
  return originLabels[value] ?? value
}

function engineTag(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'ready') return 'success'
  if (status === 'not_configured') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

onMounted(loadCatalog)
onBeforeUnmount(() => { disposed = true; requestVersion++ })
</script>

<template>
  <section class="catalog-page" v-loading="loading">
    <header class="catalog-head">
      <div>
        <h2>统一规则事实目录</h2>
        <p>统一检索规则来源、执行器、CWE/OWASP 映射与可用状态；目录不改变各执行器的写入职责。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadCatalog">刷新目录</el-button>
    </header>

    <p v-if="loadError" class="load-error" role="alert">{{ loadError }}</p>

    <template v-if="catalog">
      <div class="engine-grid">
        <article v-for="engine in catalog.engines" :key="engine.code" class="engine-card">
          <div class="engine-title">
            <strong>{{ engine.name }}</strong>
            <el-tag :type="engineTag(engine.status)" size="small">
              {{ engineStatusLabels[engine.status] ?? '状态未知' }}
            </el-tag>
          </div>
          <p>{{ engine.status_message }}</p>
          <div class="engine-meta">
            <span>套件：{{ engine.suites.join(' / ') || '—' }}</span>
            <span>版本：{{ engine.version || '未取得' }}</span>
          </div>
          <a :href="engine.documentation_url || engine.source_url" target="_blank" rel="noopener noreferrer">查看官方出处</a>
        </article>
      </div>

      <div class="source-note">
        <strong>中国漏洞库边界：</strong>{{ catalog.mapping_note }}
        <span class="source-links">
          <a v-for="source in catalog.sources" :key="source.code" :href="source.url" target="_blank" rel="noopener noreferrer">
            {{ source.name }}
          </a>
        </span>
      </div>

      <el-card shadow="never">
        <div class="catalog-toolbar">
          <el-input v-model="keyword" clearable placeholder="搜索编码、名称、CWE、OWASP、语言或说明" />
          <el-select v-model="origin" clearable placeholder="全部来源">
            <el-option v-for="(count, key) in catalog.counts" :key="key" :value="key" :label="`${originLabel(key)}（${count}）`" />
          </el-select>
          <span>显示 {{ filteredItems.length }} / {{ catalog.items.length }} 条</span>
        </div>
        <el-table :data="filteredItems" max-height="620" style="width: 100%">
          <el-table-column prop="code" label="规则编码" min-width="160" show-overflow-tooltip />
          <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
          <el-table-column label="来源" width="130">
            <template #default="{ row }">{{ originLabel(row.origin) }}</template>
          </el-table-column>
          <el-table-column label="执行器" width="145">
            <template #default="{ row }">{{ executorLabels[row.executor] ?? row.executor }}</template>
          </el-table-column>
          <el-table-column prop="language" label="语言" width="120" show-overflow-tooltip />
          <el-table-column label="映射" min-width="160">
            <template #default="{ row }">{{ [row.cwe, row.owasp].filter(Boolean).join(' · ') || '—' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="95">
            <template #default="{ row }">
              <el-tag :type="row.executable && row.enabled ? 'success' : 'info'" size="small">
                {{ row.origin === 'review_rule' ? (row.enabled ? '参与语义审查' : '已停用') : (row.executable && row.enabled ? '可执行' : '未启用') }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </section>
</template>

<style scoped lang="scss">
.catalog-page { display: grid; gap: 16px; }
.catalog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.catalog-head h2 { margin: 0 0 6px; font-size: 20px; }
.catalog-head p { margin: 0; color: var(--gray-600); }
.load-error { margin: 0; padding: 12px 14px; color: #b42318; background: #fff1f0; border-radius: 6px; }
.engine-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.engine-card { padding: 16px; border: 1px solid var(--gray-200); border-radius: 8px; background: #fff; }
.engine-card p { min-height: 44px; color: var(--gray-600); line-height: 1.6; }
.engine-title, .engine-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.engine-meta { margin-bottom: 10px; color: var(--gray-500); font-size: 12px; }
.source-note { padding: 14px 16px; border-left: 3px solid var(--primary-color, #5b58e8); background: var(--gray-50); line-height: 1.7; }
.source-links { display: inline-flex; flex-wrap: wrap; gap: 12px; margin-left: 12px; }
.catalog-toolbar { display: grid; grid-template-columns: minmax(260px, 1fr) 220px auto; align-items: center; gap: 12px; margin-bottom: 14px; }
.catalog-toolbar > span { color: var(--gray-500); white-space: nowrap; }

@media (max-width: 760px) {
  .catalog-head, .engine-title, .engine-meta { align-items: flex-start; flex-direction: column; }
  .catalog-toolbar { grid-template-columns: 1fr; }
}
</style>
