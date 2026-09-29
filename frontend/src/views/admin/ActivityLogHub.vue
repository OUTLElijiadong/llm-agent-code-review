<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'

type ActivityLogType = 'ai' | 'operations'

const AiLogList = defineAsyncComponent(() => import('./AiLogList.vue'))
const SystemAudit = defineAsyncComponent(() => import('./SystemAudit.vue'))
const route = useRoute()
const router = useRouter()

const activeType = computed<ActivityLogType>(() => {
  const queryType = String(route.query.logType || '')
  if (queryType === 'ai' || route.query.section === 'ai-logs') return 'ai'
  return 'operations'
})
const activeComponent = computed(() => activeType.value === 'ai' ? AiLogList : SystemAudit)

function selectType(value: string | number): void {
  void router.replace({
    path: route.path,
    query: { ...route.query, section: 'logs', logType: String(value) },
  })
}
</script>

<template>
  <section class="activity-log-hub" aria-label="日志与审计">
    <header class="activity-log-head">
      <p>按数据来源查看记录：AI 调用日志记录模型请求，操作审计记录账号与管理操作。</p>
      <el-tabs :model-value="activeType" @tab-change="selectType">
        <el-tab-pane name="ai" label="AI 调用日志" />
        <el-tab-pane name="operations" label="操作审计" />
      </el-tabs>
    </header>
    <component :is="activeComponent" />
  </section>
</template>

<style scoped>
.activity-log-hub { min-width: 0; }
.activity-log-head { margin-bottom: 14px; }
.activity-log-head p { margin: 0 0 8px; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
.activity-log-head :deep(.el-tabs__header) { margin-bottom: 0; }
</style>
