<script setup lang="ts">
import { ref, watch } from 'vue'
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
  <div class="support-center" data-xiaoling-page="/support" :data-xiaoling-section="activeSection">
    <div class="support-header">
      <h1>支持中心</h1>
    </div>

    <el-tabs :model-value="activeSection" class="support-tabs" @update:model-value="changeSection">
      <el-tab-pane name="maintenance" label="支持工单">
        <MaintenanceCenter />
      </el-tab-pane>
      <el-tab-pane name="feedback" label="意见反馈">
        <FeedbackCenter :active="activeSection === 'feedback'" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.support-center { padding: 4px; }
.support-header { display: flex; align-items: flex-end; gap: 16px; margin-bottom: 12px; }
.support-header h1 { margin: 0; font-size: 28px; }
.support-tabs :deep(.el-tab-pane) { padding-top: 2px; }
@media (max-width: 680px) {
  .support-header { align-items: flex-start; flex-direction: column; }
  .support-header h1 { font-size: 24px; }
}
</style>
