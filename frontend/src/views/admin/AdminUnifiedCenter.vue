<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { resolveAdminSection, sectionQuery, type AdminCenterDomain } from '@/utils/adminUnifiedNavigation'

const AdminOverview = defineAsyncComponent(() => import('./AdminOverview.vue'))
const AgentGovernance = defineAsyncComponent(() => import('./AgentGovernance.vue'))
const AgentStudio = defineAsyncComponent(() => import('@/views/agent/AgentStudio.vue'))
const ApprovalHub = defineAsyncComponent(() => import('./ApprovalHub.vue'))
const KnowledgeGovernance = defineAsyncComponent(() => import('./KnowledgeGovernance.vue'))
const SkillManager = defineAsyncComponent(() => import('./SkillManager.vue'))
const PolicyCenter = defineAsyncComponent(() => import('./PolicyCenter.vue'))
const ToolGovernance = defineAsyncComponent(() => import('./ToolGovernance.vue'))
const JobCenter = defineAsyncComponent(() => import('./JobCenter.vue'))
const ObservabilityCenter = defineAsyncComponent(() => import('./ObservabilityCenter.vue'))
const RewardCenter = defineAsyncComponent(() => import('./RewardCenter.vue'))
const RollbackCenter = defineAsyncComponent(() => import('./RollbackCenter.vue'))
const ActivityLogHub = defineAsyncComponent(() => import('./ActivityLogHub.vue'))
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

const domain = computed<AdminCenterDomain>(() => {
  const value = String(route.meta.unifiedDomain || route.path.split('/').filter(Boolean).pop() || 'agents')
  return ['agents', 'operations', 'access', 'platform'].includes(value) ? value as AdminCenterDomain : 'agents'
})

interface Tab { name: string; label: string; component: unknown; superAdmin?: boolean }
const tabs: Record<AdminCenterDomain, Tab[]> = {
  agents: [
    { name: 'agents', label: 'Agent目录', component: AgentGovernance },
    { name: 'studio', label: '创建与测试', component: AgentStudio },
    { name: 'approvals', label: '审批中心', component: ApprovalHub },
    { name: 'knowledge', label: '知识与记忆', component: KnowledgeGovernance },
    { name: 'skills', label: 'Skill', component: SkillManager },
  ],
  operations: [
    { name: 'overview', label: '运行总览', component: AdminOverview },
    { name: 'policies', label: '策略', component: PolicyCenter },
    { name: 'tools', label: '工具权限', component: ToolGovernance },
    { name: 'jobs', label: '任务调度', component: JobCenter },
    { name: 'observability', label: '监控告警', component: ObservabilityCenter },
    { name: 'logs', label: '日志与审计', component: ActivityLogHub },
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
  const alias = resolveAdminSection(domain.value, requested)
  return visibleTabs.value.some((tab) => tab.name === alias) ? alias : visibleTabs.value[0]?.name || ''
})
const activeTab = computed(() => visibleTabs.value.find((tab) => tab.name === activeName.value) || visibleTabs.value[0])
const sectionDescription = computed(() => {
  const descriptions: Record<string, string> = {
    agents: '维护 Agent 目录、创建流程和能力版本。',
    studio: '创建、测试并提交 Agent 版本。',
    approvals: '按事项类型处理发布、执行和规则提案审批。',
    knowledge: '维护 Agent 可用的知识来源与记忆。',
    skills: '查看和管理可复用技能。',
    overview: '查看平台运行状态、任务积压和服务健康。',
    policies: '配置风险策略并查看决策记录。',
    tools: '管理工具权限及执行记录。',
    jobs: '查看自动任务计划并管理调度状态。',
    observability: '查看开放告警、工具执行结果及其统计范围。',
    logs: '在模型调用与账号操作两类记录之间切换；两类数据保留各自来源和筛选口径。',
    rewards: '查看 Agent 质量反馈与奖惩记录。',
    rollback: '查看可恢复版本并执行版本回退。',
    users: '管理账号状态和角色归属。',
    roles: '管理角色授权和数据范围。',
    permissions: '查看当前接口返回的权限点目录。',
    'beta-codes': '管理内测账号和邀请码。',
    'report-templates': '管理审查报告模板。',
    llm: '管理模型连接与默认模型配置。',
    embedding: '管理知识检索的向量模型配置。',
    'mcp-workers': '管理外部工具和沙箱执行节点。',
  }
  return descriptions[activeName.value] || '选择上方分区以查看对应管理内容。'
})

function selectTab(value: string | number): void {
  void router.replace({ path: route.path, query: sectionQuery(route.query, String(value)) })
}
</script>

<template>
  <div class="unified-center">
    <header class="unified-head">
      <p class="unified-description">{{ sectionDescription }}</p>
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
.unified-head p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
@media (max-width: 860px) {
  .unified-head { align-items: stretch; flex-direction: column; gap: 8px; }
  .unified-head :deep(.el-tabs__nav-wrap) { overflow-x: auto; }
}
</style>
