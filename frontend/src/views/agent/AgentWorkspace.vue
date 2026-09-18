<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const AgentCenter = defineAsyncComponent(() => import('./AgentCenter.vue'))
const AgentStudio = defineAsyncComponent(() => import('./AgentStudio.vue'))

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

type Section = 'directory' | 'studio'

const canDirectory = computed(() => userStore.hasPermission('agent:view') || userStore.isAdmin())
const canStudio = computed(() => userStore.hasPermission('agent_asset:create') || userStore.isAdmin())
const section = computed<Section>(() => {
  if (route.query.section === 'studio' && canStudio.value) return 'studio'
  if (canDirectory.value) return 'directory'
  return 'studio'
})

function selectSection(value: string | number): void {
  const nextSection: Section = value === 'studio' ? 'studio' : 'directory'
  void router.replace({ path: '/agents', query: nextSection === 'directory' ? {} : { section: nextSection } })
}
</script>

<template>
  <div class="workspace-page">
    <header class="workspace-head">
      <div>
        <h2>Agent 工作台</h2>
        <p>统一查看可用 Agent、创建自定义 Agent，并沿用现有权限边界。</p>
      </div>
      <el-tabs :model-value="section" @tab-change="selectSection">
        <el-tab-pane v-if="canDirectory" label="Agent 目录" name="directory" />
        <el-tab-pane v-if="canStudio" label="创建与测试" name="studio" />
      </el-tabs>
    </header>

    <AgentCenter v-if="section === 'directory' && canDirectory" />
    <AgentStudio v-else-if="canStudio" />
    <el-empty v-else description="当前账号没有可访问的 Agent 功能" />
  </div>
</template>

<style scoped>
.workspace-page { min-width: 0; }
.workspace-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.workspace-head h2 { margin: 0; }
.workspace-head p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 760px) {
  .workspace-head { align-items: stretch; flex-direction: column; gap: 8px; }
}
</style>
