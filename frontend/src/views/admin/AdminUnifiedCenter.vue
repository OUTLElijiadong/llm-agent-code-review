<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const AdminOverview = defineAsyncComponent(() => import('./AdminOverview.vue'))
const AgentGovernance = defineAsyncComponent(() => import('./AgentGovernance.vue'))
const AgentStudio = defineAsyncComponent(() => import('@/views/agent/AgentStudio.vue'))
const ApprovalCenter = defineAsyncComponent(() => import('./ApprovalCenter.vue'))
const AgentReleaseAdmin = defineAsyncComponent(() => import('./AgentReleaseAdmin.vue'))
const KnowledgeGovernance = defineAsyncComponent(() => import('./KnowledgeGovernance.vue'))
const EvolutionCenter = defineAsyncComponent(() => import('./EvolutionCenter.vue'))
const SkillManager = defineAsyncComponent(() => import('./SkillManager.vue'))
const PolicyCenter = defineAsyncComponent(() => import('./PolicyCenter.vue'))
const ToolGovernance = defineAsyncComponent(() => import('./ToolGovernance.vue'))
const JobCenter = defineAsyncComponent(() => import('./JobCenter.vue'))
const ObservabilityCenter = defineAsyncComponent(() => import('./ObservabilityCenter.vue'))
const RewardCenter = defineAsyncComponent(() => import('./RewardCenter.vue'))
const RollbackCenter = defineAsyncComponent(() => import('./RollbackCenter.vue'))
const AiLogList = defineAsyncComponent(() => import('./AiLogList.vue'))
const SystemAudit = defineAsyncComponent(() => import('./SystemAudit.vue'))
const UserManage = defineAsyncComponent(() => import('./UserManage.vue'))
const RoleManage = defineAsyncComponent(() => import('./RoleManage.vue'))
const PermissionList = defineAsyncComponent(() => import('./PermissionList.vue'))
const BetaCodeAdmin = defineAsyncComponent(() => import('./BetaCodeAdmin.vue'))
const ReportTemplateManage = defineAsyncComponent(() => import('@/views/report/ReportTemplateManage.vue'))
const LlmConfig = defineAsyncComponent(() => import('./LlmConfig.vue'))
const EmbeddingConfig = defineAsyncComponent(() => import('./EmbeddingConfig.vue'))
const McpWorkerGovernance = defineAsyncComponent(() => import('./McpWorkerGovernance.vue'))

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

type Domain = 'agents' | 'operations' | 'access' | 'platform'
const domain = computed<Domain>(() => {
  const value = String(route.meta.unifiedDomain || route.path.split('/').filter(Boolean).pop() || 'agents')
  return ['agents', 'operations', 'access', 'platform'].includes(value) ? value as Domain : 'agents'
})

interface Tab { name: string; label: string; component: unknown; superAdmin?: boolean }
const tabs: Record<Domain, Tab[]> = {
  agents: [
    { name: 'agents', label: 'Agent目录', component: AgentGovernance },
    { name: 'studio', label: '创建与测试', component: AgentStudio },
    { name: 'releases', label: '发布审批', component: AgentReleaseAdmin },
    { name: 'approvals', label: '通用审批', component: ApprovalCenter },
    { name: 'knowledge', label: '知识与记忆', component: KnowledgeGovernance },
    { name: 'evolution', label: '自进化', component: EvolutionCenter },
    { name: 'skills', label: 'Skill', component: SkillManager },
  ],
  operations: [
    { name: 'overview', label: '运行总览', component: AdminOverview },
    { name: 'policies', label: '策略', component: PolicyCenter },
    { name: 'tools', label: '工具权限', component: ToolGovernance },
    { name: 'jobs', label: '任务调度', component: JobCenter },
    { name: 'observability', label: '监控告警', component: ObservabilityCenter },
    { name: 'ai-logs', label: '调用日志', component: AiLogList },
    { name: 'audit', label: '系统审计', component: SystemAudit },
    { name: 'rewards', label: '奖惩趋势', component: RewardCenter },
    { name: 'rollback', label: '版本回退', component: RollbackCenter },
  ],
  access: [
    { name: 'users', label: '用户管理', component: UserManage },
    { name: 'roles', label: '角色管理', component: RoleManage },
    { name: 'permissions', label: '权限点', component: PermissionList },
  ],
  platform: [
    { name: 'beta-codes', label: '内测码', component: BetaCodeAdmin },
    { name: 'report-templates', label: '报告模板', component: ReportTemplateManage },
    { name: 'llm', label: '大模型', component: LlmConfig, superAdmin: true },
    { name: 'embedding', label: 'RAG嵌入', component: EmbeddingConfig, superAdmin: true },
    { name: 'mcp-workers', label: 'MCP与沙箱节点', component: McpWorkerGovernance, superAdmin: true },
  ],
}

const visibleTabs = computed(() => tabs[domain.value].filter((tab) => !tab.superAdmin || userStore.isSuperAdmin()))
const activeName = computed(() => {
  const requested = String(route.query.section || '')
  return visibleTabs.value.some((tab) => tab.name === requested) ? requested : visibleTabs.value[0]?.name || ''
})
const activeTab = computed(() => visibleTabs.value.find((tab) => tab.name === activeName.value) || visibleTabs.value[0])

function selectTab(value: string | number): void {
  void router.replace({ path: route.path, query: { ...route.query, section: String(value) } })
}
</script>

<template>
  <div class="unified-center">
    <header class="unified-head">
      <div>
        <h2>{{ domain === 'agents' ? 'Agent 治理中心' : domain === 'operations' ? '运行与审计中心' : domain === 'access' ? '用户与权限中心' : '平台配置中心' }}</h2>
        <p>同一业务域集中处理，保留原有权限和操作能力。</p>
      </div>
      <el-tabs :model-value="activeName" @tab-change="selectTab">
        <el-tab-pane v-for="tab in visibleTabs" :key="tab.name" :label="tab.label" :name="tab.name" />
      </el-tabs>
    </header>
    <component :is="activeTab?.component" v-if="activeTab" />
    <el-empty v-else description="当前账号没有可访问的管理功能" />
  </div>
</template>

<style scoped>
.unified-center { min-width: 0; }
.unified-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.unified-head h2 { margin: 0; }
.unified-head p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 860px) {
  .unified-head { align-items: stretch; flex-direction: column; gap: 8px; }
  .unified-head :deep(.el-tabs__nav-wrap) { overflow-x: auto; }
}
</style>
