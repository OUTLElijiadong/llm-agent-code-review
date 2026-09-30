<script setup lang="ts">
/**
 * MetaGPT 编排可视化面板(v2.4 F2)
 *
 * 展示 MetaGPT 编排层的:
 * 1. 模块信息(版本/描述/核心组件/工厂函数)
 * 2. Environment 预览(review/discussion 两种模式切换)
 * 3. 角色拓扑卡片(每个 RoleAdapter 的 name/profile/goal/react_action/watch_actions)
 * 4. 可适配 Agent 列表(展示哪些 Agent 可加入 Environment)
 *
 * 数据来源:
 *   - GET /api/agents/metagpt/info
 *   - GET /api/agents/metagpt/preview?mode=review|discussion
 */
import { computed, onMounted, ref, watch } from 'vue'

import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getMetaGPTInfo, previewMetaGPTEnvironment } from '@/api/agent'
import { ElMessage } from 'element-plus/es/components/message/index'
import type {
  MetaGPTEnvironmentPreviewOut,
  MetaGPTInfoOut,
  MetaGPTRoleInfo,
} from '@/types/agent'

// === 状态 ===
const infoLoading = ref(false)
const previewLoading = ref(false)
const info = ref<MetaGPTInfoOut | null>(null)
const preview = ref<MetaGPTEnvironmentPreviewOut | null>(null)
const mode = ref<'review' | 'discussion'>('review')

const COMPONENT_PRESENTATION: Record<string, { name: string; description: string }> = {
  Environment: { name: '团队执行空间', description: '保存本次协作参与者、任务状态与消息。' },
  Role: { name: 'Agent 职责', description: '说明每位 Agent 在协作任务中的职责与目标。' },
  RoleAdapter: { name: 'Agent 接入配置', description: '将可用 Agent 接入审查或讨论流程。' },
  Message: { name: '协作消息', description: '在团队成员之间传递任务和阶段结果。' },
}

const FACTORY_PRESENTATION: Record<string, { name: string; description: string }> = {
  build_review_environment: { name: '代码审查团队', description: '组合代码审查所需的 Agent，并按审查任务传递结果。' },
  build_discussion_environment: { name: '圆桌讨论团队', description: '组合圆桌讨论参与者，并协调各轮发言。' },
}

const AGENT_PRESENTATION: Record<string, string> = {
  code_reviewer: '代码质量审查 Agent',
  security_sentinel: '安全审查 Agent',
}

const CATEGORY_PRESENTATION: Record<string, string> = {
  review: '审查',
  security: '安全',
  performance: '性能',
  reliability: '可靠性',
  operations: '运维',
  general: '通用',
}

// === 角色状态中文标签 ===
const ROLE_STATE_LABELS: Record<string, string> = {
  idle: '空闲',
  thinking: '思考中',
  acting: '执行中',
  done: '已完成',
  error: '错误',
}

const ROLE_STATE_TYPES: Record<string, string> = {
  idle: 'info',
  thinking: 'warning',
  acting: 'warning',
  done: 'success',
  error: 'danger',
}

/**
 * 角色状态中文标签
 * @param state - 角色状态英文标识
 * @returns 中文标签
 */
function roleStateLabel(state: string): string {
  return ROLE_STATE_LABELS[state] ?? state
}

/**
 * 角色状态 el-tag type
 * @param state - 角色状态英文标识
 * @returns el-tag type 属性值
 */
function roleStateType(state: string): string {
  return ROLE_STATE_TYPES[state] ?? 'info'
}

/**
 * 加载 MetaGPT 模块信息
 * 调用 GET /api/agents/metagpt/info,失败时显示提示。
 */
async function loadInfo(): Promise<void> {
  infoLoading.value = true
  try {
    info.value = await getMetaGPTInfo()
  } catch {
    ElMessage.error('加载 Agent 协作信息失败')
    info.value = null
  } finally {
    infoLoading.value = false
  }
}

/**
 * 加载 Environment 预览
 * 调用 GET /api/agents/metagpt/preview?mode=xxx,失败时显示提示。
 */
async function loadPreview(): Promise<void> {
  previewLoading.value = true
  try {
    preview.value = await previewMetaGPTEnvironment(mode.value)
  } catch {
    ElMessage.error('加载执行方案预览失败')
    preview.value = null
  } finally {
    previewLoading.value = false
  }
}

/**
 * 刷新全部数据(信息 + 预览)
 */
async function refreshAll(): Promise<void> {
  await Promise.all([loadInfo(), loadPreview()])
  ElMessage.success('已同步最新 Agent 协作数据')
}

/**
 * 切换环境模式并重新加载预览
 */
async function switchMode(m: 'review' | 'discussion'): Promise<void> {
  if (m === mode.value) return
  mode.value = m
  await loadPreview()
}

// === 计算属性 ===

/**
 * 核心组件列表(转为数组便于 v-for)
 */
const componentList = computed(() => {
  if (!info.value?.components) return []
  return Object.entries(info.value.components).map(([key]) => COMPONENT_PRESENTATION[key] ?? {
    name: '协作组件',
    description: '参与 Agent 团队的执行与消息协调。',
  })
})

/**
 * 工厂函数列表(转为数组便于 v-for)
 */
const factoryList = computed(() => {
  if (!info.value?.factories) return []
  return Object.entries(info.value.factories).map(([key]) => FACTORY_PRESENTATION[key] ?? {
    name: '内置协作方案',
    description: '供审查或讨论任务调用的 Agent 团队配置。',
  })
})

/**
 * 角色列表(从 preview 中取)
 */
const roles = computed<MetaGPTRoleInfo[]>(() => preview.value?.roles ?? [])

/**
 * 可适配 Agent 列表(从 info 中取)
 */
const adaptableAgents = computed(() => (info.value?.adaptable_agents ?? []).map((agent) => ({
  ...agent,
  displayName: agentDisplayName(agent.name),
  categoryLabel: CATEGORY_PRESENTATION[agent.category] || '通用',
})))

/**
 * 默认参与当前模式的 Agent 列表
 */
const defaultAgents = computed(() => {
  if (!info.value) return []
  const codes = mode.value === 'review'
    ? info.value.default_review_agents
    : info.value.default_discussion_agents
  return codes.map(agentDisplayName)
})

function agentDisplayName(code: string): string {
  const known = AGENT_PRESENTATION[code]
  if (known) return known
  const description = info.value?.adaptable_agents.find((agent) => agent.name === code)?.description?.trim()
  return description || '协作 Agent'
}

function roleDisplayName(role: MetaGPTRoleInfo): string {
  const code = role.agent_name || role.name
  return AGENT_PRESENTATION[code] || role.profile?.trim() || agentDisplayName(code)
}

function roleGoal(role: MetaGPTRoleInfo): string {
  const code = role.agent_name || role.name
  return (role.goal || '为当前任务提供专业分析')
    .replace(code, agentDisplayName(code))
    .replace(/^完成\s+/, '完成')
}

function messageActionLabel(action: string): string {
  const known: Record<string, string> = {
    StartReview: '收到审查开始消息',
    CrossReview: '收到交叉复核消息',
    DiscussTurn: '收到新一轮讨论消息',
    StartDiscussion: '收到讨论开始消息',
  }
  if (known[action]) return known[action]
  if (action.endsWith('_Reply')) return '收到其他 Agent 的审查结果'
  if (action.endsWith('_Discuss')) return '收到其他 Agent 的讨论观点'
  return '收到团队协作消息'
}

function outboundActionLabel(action: string): string {
  if (action.endsWith('_Reply')) return '完成分析后提交审查结果'
  if (action.endsWith('_Discuss')) return '完成发言后提交讨论观点'
  return '完成分析后向团队提交结果'
}

// === 生命周期 ===

onMounted(() => {
  refreshAll()
})

// 模式变化时重新加载预览(由 switchMode 触发,这里不重复)
watch(mode, () => {
  // watch 仅用于响应外部直接修改 mode 的情况(如 devtools)
  loadPreview()
})
</script>

<template>
  <div class="metagpt-panel">
    <!-- 顶部:模块信息 -->
    <section class="info-section">
      <header class="section-head">
        <div>
          <h3 class="section-title">Agent 协作能力</h3>
          <p v-if="info" class="section-sub">
            <el-tag size="small" type="success">内置协作方案</el-tag>
            <span class="section-desc">展示可用于审查与讨论的 Agent 协作方式；预览不会启动模型调用。</span>
          </p>
        </div>
        <div class="section-actions">
          <el-button :loading="infoLoading || previewLoading" @click="refreshAll">
            刷新
          </el-button>
        </div>
      </header>

      <PrismLoading
        v-if="infoLoading && !info"
        label="正在读取协作能力"
        sublabel="获取可用于审查和讨论的团队配置"
      />

      <template v-else-if="info">
        <!-- 核心组件 -->
        <div class="components-grid">
          <article
            v-for="comp in componentList"
            :key="comp.name"
            class="component-card"
          >
            <code class="component-name">{{ comp.name }}</code>
            <p class="component-desc">{{ comp.description }}</p>
          </article>
        </div>

        <!-- 工厂函数 -->
        <div class="factories-block">
          <div class="block-label">内置协作方案</div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item
              v-for="fac in factoryList"
              :key="fac.name"
              :label="fac.name"
            >
              {{ fac.description }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </template>

      <EmptyState
        v-else
        description="协作能力读取失败，请刷新重试"
        compact
      />
    </section>

    <!-- 中部:协作方案预览 -->
    <section class="preview-section">
      <header class="section-head">
        <div>
          <h3 class="section-title">团队执行结构</h3>
          <p class="section-sub">
            预览不会调用模型，只展示参与角色、职责和消息接收关系。
          </p>
        </div>
        <div class="mode-switch">
          <el-radio-group :model-value="mode" size="small" @change="switchMode">
            <el-radio-button value="review">审查环境</el-radio-button>
            <el-radio-button value="discussion">讨论环境</el-radio-button>
          </el-radio-group>
        </div>
      </header>

      <PrismLoading
        v-if="previewLoading && !preview"
        label="正在生成团队预览"
        sublabel="根据当前模式整理角色和协作关系"
      />

      <template v-else-if="preview">
        <!-- 团队方案信息 -->
        <div class="env-meta">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="协作方案">
              {{ mode === 'review' ? '代码审查团队' : '圆桌讨论团队' }}
            </el-descriptions-item>
            <el-descriptions-item label="最多协作轮次">
              {{ preview.max_depth }} 轮
            </el-descriptions-item>
            <el-descriptions-item label="参与角色">
              {{ preview.roles.length }}
            </el-descriptions-item>
            <el-descriptions-item label="可用内置 Agent">
              {{ preview.registered_agent_count }}
            </el-descriptions-item>
            <el-descriptions-item label="默认参与">
              <el-tag
                v-for="ag in defaultAgents"
                :key="ag"
                size="small"
                type="info"
                style="margin-right: 4px"
              >
                {{ ag }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <!-- 角色拓扑卡片 -->
        <div v-if="roles.length" class="roles-grid">
          <article
            v-for="role in roles"
            :key="role.name"
            class="role-card"
          >
            <header class="role-head">
              <div class="role-avatar" :style="{ background: role.agent_color || 'var(--brand-500)' }">
                {{ roleDisplayName(role).charAt(0) }}
              </div>
              <div class="role-meta">
                <div class="role-name">{{ roleDisplayName(role) }}</div>
              </div>
              <el-tag
                size="small"
                :type="roleStateType(role.state)"
                effect="plain"
              >
                {{ roleStateLabel(role.state) }}
              </el-tag>
            </header>

            <div class="role-body">
              <div class="role-field">
                <span class="field-label">目标</span>
                <span class="field-value">{{ roleGoal(role) }}</span>
              </div>
              <div class="role-field">
                <span class="field-label">约束</span>
                <span class="field-value">{{ role.constraints || '—' }}</span>
              </div>
              <div class="role-field">
              <span class="field-label">响应规则</span>
                <span class="field-value">{{ outboundActionLabel(role.react_action) }}</span>
              </div>
              <div class="role-field">
              <span class="field-label">接收消息</span>
                <div v-if="role.watch_actions.length" class="watch-tags">
                  <el-tag
                    v-for="act in role.watch_actions"
                    :key="act"
                    size="small"
                    type="warning"
                    effect="plain"
                  >
                    {{ messageActionLabel(act) }}
                  </el-tag>
                </div>
                <span v-else class="field-value field-empty">接收全部</span>
              </div>
              <div class="role-field">
                <span class="field-label">记忆</span>
                <span class="field-value">{{ role.memory_size }} 条消息</span>
              </div>
            </div>

            <footer v-if="role.agent_description" class="role-foot">
              <span class="foot-label">Agent 描述:</span>
              <span class="foot-desc">{{ role.agent_description }}</span>
            </footer>
          </article>
        </div>

        <EmptyState
          v-else
          description="当前方案中没有可用 Agent，请刷新；如持续出现，请联系管理员检查目录配置。"
          compact
        />
      </template>

      <EmptyState
        v-else
        description="团队方案加载失败，请刷新重试"
        compact
      />
    </section>

    <!-- 底部:可适配 Agent 列表 -->
    <section v-if="adaptableAgents.length" class="adaptable-section">
      <header class="section-head">
        <h3 class="section-title">可加入团队的内置 Agent</h3>
        <p class="section-sub">
          共 {{ adaptableAgents.length }} 个内置 Agent 可加入团队；不包含已发布的自定义 Agent 和历史审查画像。
        </p>
      </header>
      <div class="adaptable-grid">
        <article
          v-for="ag in adaptableAgents"
          :key="ag.name"
          class="adaptable-card"
        >
          <div class="adaptable-avatar" :style="{ background: ag.color || 'var(--gray-400)' }">
            {{ ag.displayName.charAt(0) }}
          </div>
          <div class="adaptable-info">
            <div class="adaptable-name">{{ ag.displayName }}</div>
            <div class="adaptable-desc">{{ ag.description }}</div>
            <el-tag size="small" type="info" effect="plain">
              {{ ag.categoryLabel }}
            </el-tag>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.metagpt-panel {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.section-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.section-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
}

.section-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--gray-500);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.section-desc {
  color: var(--gray-600);
}

.section-actions {
  display: flex;
  gap: 8px;
}

/* === 模块信息区 === */
.info-section,
.preview-section,
.adaptable-section {
  background: var(--surface-1);
  border: var(--hairline);
  border-radius: 12px;
  padding: 20px;
}

.components-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.component-card {
  background: var(--surface-2, #FAFBFC);
  border: 1px solid var(--gray-150, #EEF0F4);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.component-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--brand-600, #5B58E8);
  font-family: var(--font-mono, monospace);
}

.component-desc {
  margin: 0;
  font-size: 12px;
  color: var(--gray-600);
  line-height: 1.5;
}

.factories-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block-label {
  font-size: 12px;
  color: var(--gray-500);
  letter-spacing: 0.04em;
}

/* === Environment 预览区 === */
.env-meta {
  margin-bottom: 16px;
}

.trace-id {
  font-size: 11px;
  color: var(--gray-500);
  word-break: break-all;
}

.roles-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}

.role-card {
  background: var(--surface-2, #FAFBFC);
  border: 1px solid var(--gray-150, #EEF0F4);
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;

  &:hover {
    border-color: var(--brand-300, #B5B2F5);
    box-shadow: var(--shadow-1, 0 1px 3px rgba(0, 0, 0, 0.04));
  }
}

.role-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.role-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}

.role-meta {
  flex: 1;
  min-width: 0;
}

.role-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--gray-900);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.role-code {
  display: block;
  font-size: 11px;
  color: var(--gray-500);
  font-family: var(--font-mono, monospace);
}

.role-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.role-field {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
}

.field-label {
  flex-shrink: 0;
  width: 56px;
  color: var(--gray-500);
}

.field-value {
  flex: 1;
  color: var(--gray-700);
  line-height: 1.5;
  word-break: break-word;
}

.field-empty {
  color: var(--gray-400);
  font-style: italic;
}

.field-code {
  font-size: 11px;
  color: var(--brand-600, #5B58E8);
  font-family: var(--font-mono, monospace);
  background: rgba(91, 88, 232, 0.06);
  padding: 1px 6px;
  border-radius: 3px;
}

.watch-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  flex: 1;
}

.role-foot {
  border-top: 1px dashed var(--gray-150, #EEF0F4);
  padding-top: 8px;
  font-size: 11.5px;
  color: var(--gray-500);
  line-height: 1.5;
}

.foot-label {
  font-weight: 600;
  margin-right: 4px;
}

.foot-desc {
  color: var(--gray-600);
}

/* === 可适配 Agent 区 === */
.adaptable-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
}

.adaptable-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--surface-2, #FAFBFC);
  border: 1px solid var(--gray-150, #EEF0F4);
  border-radius: 8px;
  padding: 10px 12px;
}

.adaptable-avatar {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}

.adaptable-info {
  flex: 1;
  min-width: 0;
}

.adaptable-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--gray-900);
}

.adaptable-desc {
  font-size: 11px;
  color: var(--gray-500);
  margin: 2px 0 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
