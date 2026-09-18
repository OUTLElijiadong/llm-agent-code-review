<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import MaintenanceCenter from './MaintenanceCenter.vue'
import FeedbackCenter from './FeedbackCenter.vue'

type SupportSection = 'maintenance' | 'feedback'

const route = useRoute()
const router = useRouter()

function sectionFromQuery(value: unknown): SupportSection {
  return value === 'feedback' ? 'feedback' : 'maintenance'
}

const activeSection = ref<SupportSection>(sectionFromQuery(route.query.section))
const activeTitle = computed(() => activeSection.value === 'maintenance' ? '维修工单' : '意见反馈')

watch(() => route.query.section, (value) => {
  const next = sectionFromQuery(value)
  if (next !== activeSection.value) activeSection.value = next
})

function changeSection(value: string | number): void {
  const section = sectionFromQuery(value)
  activeSection.value = section
  void router.replace({ query: { ...route.query, section } })
}
</script>

<template>
  <div class="support-center">
    <div class="support-header">
      <div>
        <div class="eyebrow">PRISM / 支持中心</div>
        <h1>支持中心</h1>
        <p>统一提交平台问题、维修申请和产品反馈，并在同一处查看处理进度。</p>
      </div>
      <el-tag effect="plain" type="info">当前：{{ activeTitle }}</el-tag>
    </div>

    <el-tabs :model-value="activeSection" class="support-tabs" @update:model-value="changeSection">
      <el-tab-pane name="maintenance" label="申请维修">
        <MaintenanceCenter />
      </el-tab-pane>
      <el-tab-pane name="feedback" label="意见反馈">
        <FeedbackCenter />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.support-center { padding: 4px; }
.support-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.eyebrow { color: var(--el-color-primary); font-size: 12px; letter-spacing: .08em; }
.support-header h1 { margin: 5px 0 4px; font-size: 28px; }
.support-header p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.support-tabs :deep(.el-tab-pane) { padding-top: 2px; }
@media (max-width: 680px) {
  .support-header { align-items: flex-start; flex-direction: column; }
  .support-header h1 { font-size: 24px; }
}
</style>
