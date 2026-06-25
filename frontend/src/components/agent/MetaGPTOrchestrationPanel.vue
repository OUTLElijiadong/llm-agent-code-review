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
import { ElMessage } from 'element-plus'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getMetaGPTInfo, previewMetaGPTEnvironment } from '@/api/agent'
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
    ElMessage.error('加载 MetaGPT 信息失败')
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
    ElMessage.error('加载 Environment 预览失败')
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
  ElMessage.success('已同步最新 MetaGPT 编排数据')
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
  return Object.entries(info.value.components).map(([name, desc]) => ({ name, desc }))
})

/**
 * 工厂函数列表(转为数组便于 v-for)
 */
const factoryList = computed(() => {
  if (!info.value?.factories) return []
  return Object.entries(info.value.factories).map(([name, desc]) => ({ name, desc }))
})

/**
 * 角色列表(从 preview 中取)
 */
const roles = computed<MetaGPTRoleInfo[]>(() => preview.value?.roles ?? [])

/**
 * 可适配 Agent 列表(从 info 中取)
 */
const adaptableAgents = computed(() => info.value?.adaptable_agents ?? [])

/**
 * 默认参与当前模式的 Agent 列表
 */
const defaultAgents = computed(() => {
  if (!info.value) return []
  return mode.value === 'review'
    ? info.value.default_review_agents
    : info.value.default_discussion_agents
})

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
          <h3 class="section-title">MetaGPT 编排层</h3>
          <p v-if="info" class="section-sub">
            <el-tag size="small" type="success">{{ info.version }}</el-tag>
            <span class="section-desc">{{ info.description }}</span>
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
        label="加载 MetaGPT 模块信息"
        sublabel="从后端拉取编排层元数据"
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
            <p class="component-desc">{{ comp.desc }}</p>
          </article>
        </div>

        <!-- 工厂函数 -->
        <div class="factories-block">
          <div class="block-label">可用工厂函数</div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item
              v-for="fac in factoryList"
              :key="fac.name"
              :label="fac.name"
            >
              {{ fac.desc }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </template>

      <EmptyState
        v-else
        description="MetaGPT 模块信息加载失败"
        compact
      />
    </section>

    <!-- 中部:Environment 预览 -->
    <section class="preview-section">
      <header class="section-head">
        <div>
          <h3 class="section-title">Environment 编排拓扑</h3>
          <p class="section-sub">
            预览模式不触发 LLM 调用,仅展示角色配置与订阅关系
          </p>
        </div>
        <div class="mode-switch">
          <el-radio-group :model-value="mode" size="small" @change="switchMode">
            <el-radio-button label="review">审查环境</el-radio-button>
            <el-radio-button label="discussion">讨论环境</el-radio-button>
          </el-radio-group>
        </div>
      </header>

      <PrismLoading
        v-if="previewLoading && !preview"
        label="构建 Environment 预览"
        sublabel="实例化 RoleAdapter 并组装拓扑"
      />

      <template v-else-if="preview">
        <!-- Environment 元信息 -->
        <div class="env-meta">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="环境名称">
              <code>{{ preview.env_name }}</code>
            </el-descriptions-item>
            <el-descriptions-item label="追踪 ID">
              <code class="trace-id">{{ preview.trace_id }}</code>
            </el-descriptions-item>
            <el-descriptions-item label="最大深度">
              {{ preview.max_depth }}
            </el-descriptions-item>
            <el-descriptions-item label="角色数">
              {{ preview.roles.length }}
            </el-descriptions-item>
            <el-descriptions-item label="已注册 Agent">
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
                {{ role.name.charAt(0).toUpperCase() }}
              </div>
              <div class="role-meta">
                <div class="role-name">{{ role.profile || role.name }}</div>
                <code class="role-code">{{ role.name }}</code>
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
                <span class="field-value">{{ role.goal || '—' }}</span>
              </div>
              <div class="role-field">
                <span class="field-label">约束</span>
                <span class="field-value">{{ role.constraints || '—' }}</span>
              </div>
              <div class="role-field">
                <span class="field-label">反应动作</span>
                <code class="field-code">{{ role.react_action }}</code>
              </div>
              <div class="role-field">
                <span class="field-label">订阅动作</span>
                <div v-if="role.watch_actions.length" class="watch-tags">
                  <el-tag
                    v-for="act in role.watch_actions"
                    :key="act"
                    size="small"
                    type="warning"
                    effect="plain"
                  >
                    {{ act }}
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
          description="当前环境无角色,请检查 AgentRegistry 注册状态"
          compact
        />
      </template>

      <EmptyState
        v-else
        description="Environment 预览加载失败"
        compact
      />
    </section>

    <!-- 底部:可适配 Agent 列表 -->
    <section v-if="adaptableAgents.length" class="adaptable-section">
      <header class="section-head">
        <h3 class="section-title">可适配 Agent 池</h3>
        <p class="section-sub">
          共 {{ adaptableAgents.length }} 个 Agent 可通过 RoleAdapter 加入 Environment
        </p>
      </header>
      <div class="adaptable-grid">
        <article
          v-for="ag in adaptableAgents"
          :key="ag.name"
          class="adaptable-card"
        >
          <div class="adaptable-avatar" :style="{ background: ag.color || 'var(--gray-400)' }">
            {{ ag.name.charAt(0).toUpperCase() }}
          </div>
          <div class="adaptable-info">
            <div class="adaptable-name">{{ ag.name }}</div>
            <div class="adaptable-desc">{{ ag.description }}</div>
            <el-tag size="small" type="info" effect="plain">
              {{ ag.category }}
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
