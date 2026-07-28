<template>
  <div class="skill-manager-page">
    <!-- 页头 -->
    <div class="page-header">
      <div>
        <h2>Skill 管理中心</h2>
        <p class="page-sub">
          v3.0 AgentSkill 升级 · 每个 Agent 挂载 self_improvement + proactive 两类专属 Skill ·
          支持手动调用、查看元数据、追溯调用记录
        </p>
      </div>
      <div class="header-actions">
        <el-button :icon="Refresh" :loading="loading" @click="reloadAll">刷新</el-button>
      </div>
    </div>

    <!-- 顶部统计 -->
    <div class="stat-grid">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">注册 Agent</div>
        <div class="stat-value">{{ runtimeAgents.length }}</div>
        <div class="stat-foot">AgentRegistry 实时同步</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">挂载 Skill</div>
        <div class="stat-value">{{ totalSkills }}</div>
        <div class="stat-foot">每个 Agent × 2(self_improve + proactive)</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">近期调用</div>
        <div class="stat-value">{{ records.length }}</div>
        <div class="stat-foot">最近 {{ recordsLimit }} 条记录</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">成功率</div>
        <div class="stat-value" :class="successRateClass">{{ successRatePct }}</div>
        <div class="stat-foot">成功 {{ successCount }} / 失败 {{ failedCount }}</div>
      </el-card>
    </div>

    <!-- Agent 选择 + Skill 列表 + 手动调用 -->
    <el-card shadow="never" class="main-card">
      <template #header>
        <div class="card-head">
          <div class="card-title">Skill 列表与手动调用</div>
          <div class="card-actions">
            <el-select
              v-model="selectedAgentName"
              placeholder="选择 Agent"
              filterable
              style="width: 260px"
              @change="onAgentChange"
            >
              <el-option
                v-for="a in runtimeAgents"
                :key="a.code"
                :label="`${a.name} (${a.code})`"
                :value="a.code"
              />
            </el-select>
          </div>
        </div>
      </template>

      <PrismLoading
        v-if="skillsLoading"
        label="加载 Skill 元数据"
        sublabel="从 SkillRegistry 拉取 per-Agent 专属 Skill"
        compact
      />

      <template v-else-if="selectedAgentName && agentSkills.length">
        <div class="skill-grid">
          <div
            v-for="sk in agentSkills"
            :key="sk.name"
            class="skill-block"
            :class="`skill-${sk.type}`"
          >
            <div class="skill-block-head">
              <span class="skill-type-badge" :class="`badge-${sk.type}`">
                {{ skillTypeLabel(sk.type) }}
              </span>
              <code class="skill-block-name">{{ sk.name }}</code>
              <el-tag size="small" :type="sk.invocable ? 'success' : 'info'" effect="plain">
                {{ sk.invocable ? '可手动调用' : '仅自动触发' }}
              </el-tag>
            </div>
            <p class="skill-block-desc">{{ sk.description }}</p>

            <!-- 手动调用表单(仅 invocable Skill 显示) -->
            <div v-if="sk.invocable" class="invoke-form">
              <el-input
                v-model="invokeForm[sk.name].action"
                size="small"
                placeholder="action(可选,如 evolve / check_proactive)"
                style="width: 240px"
              />
              <el-input
                v-model="invokeForm[sk.name].paramsJson"
                size="small"
                type="textarea"
                :rows="2"
                placeholder='params JSON(可选,如 {"window_days": 90})'
                style="flex: 1; min-width: 200px"
              />
              <el-button
                type="primary"
                size="small"
                :loading="invoking[sk.name]"
                @click="onInvokeSkill(sk)"
              >
                调用
              </el-button>
            </div>

            <!-- 调用结果展示 -->
            <div
              v-if="invokeResults[sk.name]"
              class="invoke-result"
              :class="invokeResults[sk.name]?.success ? 'result-ok' : 'result-bad'"
            >
              <div class="result-head">
                <span class="result-status">{{ invokeResults[sk.name]?.success ? '✓ 成功' : '✗ 失败' }}</span>
                <span v-if="invokeResults[sk.name]?.effect" class="result-effect">
                  效果: {{ invokeResults[sk.name]?.effect }}
                </span>
                <span v-if="invokeResults[sk.name]?.duration_ms" class="result-duration">
                  耗时 {{ invokeResults[sk.name]?.duration_ms }}ms
                </span>
                <span v-if="invokeResults[sk.name]?.record_id" class="result-record">
                  记录 #{{ invokeResults[sk.name]?.record_id }}
                </span>
              </div>
              <pre v-if="invokeResults[sk.name]?.data" class="result-json">{{ prettyResult(invokeResults[sk.name]?.data) }}</pre>
              <p v-if="invokeResults[sk.name]?.error" class="result-error">{{ invokeResults[sk.name]?.error }}</p>
            </div>
          </div>
        </div>
      </template>

      <EmptyState
        v-else
        :description="selectedAgentName ? '该 Agent 暂未挂载 Skill' : '请先选择一个 Agent'"
      />
    </el-card>

    <!-- 调用记录 -->
    <el-card shadow="never" class="main-card">
      <template #header>
        <div class="card-head">
          <div class="card-title">Skill 调用记录</div>
          <div class="card-actions">
            <el-select
              v-model="filterAgentName"
              placeholder="按 Agent 过滤"
              clearable
              filterable
              style="width: 180px"
              @change="loadRecords"
            >
              <el-option
                v-for="a in runtimeAgents"
                :key="a.code"
                :label="a.name"
                :value="a.code"
              />
            </el-select>
            <el-select
              v-model="filterTriggerType"
              placeholder="按触发类型"
              clearable
              style="width: 140px"
              @change="loadRecords"
            >
              <el-option label="手动" value="manual" />
              <el-option label="定时" value="scheduled" />
              <el-option label="事件" value="event" />
              <el-option label="调度" value="orchestrator" />
            </el-select>
            <el-button :icon="Refresh" :loading="recordsLoading" @click="loadRecords">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="recordsLoading"
        :data="records"
        stripe
        empty-text="暂无 Skill 调用记录"
        max-height="480"
      >
        <el-table-column label="Agent" width="160">
          <template #default="{ row }">
            <code class="rec-agent">{{ row.agent_name }}</code>
          </template>
        </el-table-column>
        <el-table-column label="Skill" min-width="200">
          <template #default="{ row }">
            <code class="rec-skill">{{ row.skill_name }}</code>
          </template>
        </el-table-column>
        <el-table-column label="触发类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="triggerTypeTag(row.trigger_type)">
              {{ triggerTypeLabel(row.trigger_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="效果" width="100">
          <template #default="{ row }">
            <span :class="effectClass(row.effect)">{{ row.effect || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">{{ row.duration_ms ? `${row.duration_ms}ms` : '-' }}</template>
        </el-table-column>
        <el-table-column label="触发来源" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-muted">{{ row.trigger_source || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="输出摘要" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ row.output_summary || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ row.create_time ? formatDateTime(row.create_time) : '-' }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { Refresh } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'
import PrismLoading from '@/components/common/PrismLoading.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  listRuntimeAgents,
  listAgentSkills,
  invokeAgentSkill,
  listSkillRecords,
} from '@/api/agent'
import type {
  AgentRuntimeOut,
  AgentSkillRecordOut,
  SkillMetaOut,
  SkillInvokeOut,
  SkillType,
} from '@/types/agent'

/**
 * Skill 类型中文标签
 * @param t - Skill 类型
 * @returns 中文标签
 */
function skillTypeLabel(t: SkillType): string {
  return t === 'self_improvement' ? '自我进化' : '主动监测'
}

/**
 * 触发类型中文标签
 * @param t - 触发类型
 * @returns 中文标签
 */
function triggerTypeLabel(t: string): string {
  const map: Record<string, string> = {
    manual: '手动',
    scheduled: '定时',
    event: '事件',
    orchestrator: '调度',
  }
  return map[t] ?? t
}

/**
 * 触发类型 el-tag type
 * @param t - 触发类型
 * @returns el-tag type
 */
function triggerTypeTag(t: string): 'primary' | 'success' | 'warning' | 'info' | 'danger' {
  const map: Record<string, 'primary' | 'success' | 'warning' | 'info' | 'danger'> = {
    manual: 'warning',
    scheduled: 'info',
    event: 'primary',
    orchestrator: 'success',
  }
  return map[t] ?? 'info'
}

/**
 * 效果标记样式类
 * @param e - 效果标记
 * @returns CSS 类名
 */
function effectClass(e?: string | null): string {
  if (e === 'success') return 'gate-ok'
  if (e === 'failed') return 'gate-bad'
  if (e === 'no_change') return 'text-muted'
  return ''
}

const loading = ref(false)
const runtimeAgents = ref<AgentRuntimeOut[]>([])

// === Skill 元数据 ===
const selectedAgentName = ref<string>('')
const agentSkills = ref<SkillMetaOut[]>([])
const skillsLoading = ref(false)

// === 手动调用 ===
const invokeForm = reactive<Record<string, { action: string; paramsJson: string }>>({})
const invoking = reactive<Record<string, boolean>>({})
const invokeResults = reactive<Record<string, SkillInvokeOut | null>>({})

// === 调用记录 ===
const records = ref<AgentSkillRecordOut[]>([])
const recordsLoading = ref(false)
const recordsLimit = 30
const filterAgentName = ref<string>('')
const filterTriggerType = ref<string>('')

const totalSkills = computed(() => runtimeAgents.value.length * 2)
const successCount = computed(() => records.value.filter((r) => r.success).length)
const failedCount = computed(() => records.value.filter((r) => !r.success).length)
const successRatePct = computed(() => {
  if (!records.value.length) return '-'
  return `${((successCount.value / records.value.length) * 100).toFixed(1)}%`
})
const successRateClass = computed(() => {
  if (!records.value.length) return ''
  const rate = successCount.value / records.value.length
  if (rate >= 0.8) return 'ok'
  if (rate >= 0.5) return 'warn'
  return 'bad'
})

/**
 * 加载 AgentRegistry 中的所有 Agent
 */
async function loadRuntimeAgents(): Promise<void> {
  try {
    runtimeAgents.value = await listRuntimeAgents()
  } catch {
    ElMessage.error('加载 Agent 列表失败')
  }
}

/**
 * Agent 选择变化时加载该 Agent 的 Skill 元数据
 */
function onAgentChange(): void {
  if (selectedAgentName.value) {
    loadAgentSkills()
  } else {
    agentSkills.value = []
  }
}

/**
 * 加载选中 Agent 的 Skill 元数据
 */
async function loadAgentSkills(): Promise<void> {
  if (!selectedAgentName.value) return
  skillsLoading.value = true
  agentSkills.value = []
  try {
    const skills = await listAgentSkills(selectedAgentName.value)
    agentSkills.value = skills
    // 初始化调用表单
    for (const sk of skills) {
      if (!(sk.name in invokeForm)) {
        invokeForm[sk.name] = { action: '', paramsJson: '' }
        invoking[sk.name] = false
        invokeResults[sk.name] = null
      }
    }
  } catch {
    ElMessage.error('加载 Skill 元数据失败')
  } finally {
    skillsLoading.value = false
  }
}

/**
 * 手动调用指定 Skill
 * 解析 paramsJson 为 JSON,调用 invokeAgentSkill,展示结果
 * @param sk - Skill 元数据
 */
async function onInvokeSkill(sk: SkillMetaOut): Promise<void> {
  invoking[sk.name] = true
  invokeResults[sk.name] = null
  try {
    let params: Record<string, unknown> = {}
    const paramsJsonStr = invokeForm[sk.name].paramsJson.trim()
    if (paramsJsonStr) {
      try {
        params = JSON.parse(paramsJsonStr)
      } catch {
        ElMessage.error('params JSON 解析失败,请检查格式')
        invoking[sk.name] = false
        return
      }
    }
    const result = await invokeAgentSkill(selectedAgentName.value, sk.name, {
      action: invokeForm[sk.name].action || undefined,
      params,
    })
    invokeResults[sk.name] = result
    if (result.success) {
      ElMessage.success(`Skill 调用成功(${result.duration_ms ?? 0}ms)`)
    } else {
      ElMessage.warning(`Skill 调用失败: ${result.error || '未知错误'}`)
    }
    // 刷新调用记录
    await loadRecords()
  } catch {
    ElMessage.error('Skill 调用异常')
  } finally {
    invoking[sk.name] = false
  }
}

/**
 * 格式化调用结果数据为可读 JSON
 * @param data - 调用结果 data
 * @returns 格式化后的 JSON 字符串
 */
function prettyResult(data: unknown): string {
  if (data === null || data === undefined) return '(空)'
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

/**
 * 加载 Skill 调用记录(支持 agentName / triggerType 过滤)
 */
async function loadRecords(): Promise<void> {
  recordsLoading.value = true
  try {
    records.value = await listSkillRecords({
      agentName: filterAgentName.value || undefined,
      triggerType: filterTriggerType.value || undefined,
      limit: recordsLimit,
    })
  } catch {
    ElMessage.error('加载 Skill 调用记录失败')
  } finally {
    recordsLoading.value = false
  }
}

/**
 * 刷新所有数据
 */
async function reloadAll(): Promise<void> {
  loading.value = true
  try {
    await Promise.all([loadRuntimeAgents(), loadRecords()])
    if (selectedAgentName.value) {
      await loadAgentSkills()
    }
  } finally {
    loading.value = false
  }
}

onMounted(reloadAll)
</script>

<style scoped lang="scss">
.skill-manager-page {
  padding: var(--spacing-lg, 24px);
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: var(--spacing-lg, 24px);

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
    max-width: 760px;
  }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

.stat-card {
  .stat-label {
    font-size: 13px;
    color: var(--color-text-secondary, #909399);
  }
  .stat-value {
    font-size: 28px;
    font-weight: 600;
    margin: 6px 0 4px;
    &.ok { color: #2f9e44; }
    &.warn { color: #e8a33d; }
    &.bad { color: #e5484d; }
  }
  .stat-foot {
    font-size: 12px;
    color: var(--color-text-secondary, #909399);
  }
}

.main-card {
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900, #161A24);
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.skill-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 16px;
}

.skill-block {
  background: var(--surface-1, #fff);
  border: 1px solid var(--color-border-light, #EEF0F4);
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;

  &.skill-self_improvement {
    border-left: 3px solid var(--brand-500, #5B58E8);
  }
  &.skill-proactive {
    border-left: 3px solid #2BBFB9;
  }
}

.skill-block-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.skill-type-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 600;

  &.badge-self_improvement {
    background: rgba(91, 88, 232, 0.10);
    color: var(--brand-600, #5B58E8);
  }
  &.badge-proactive {
    background: rgba(43, 191, 185, 0.12);
    color: #1A8F8A;
  }
}

.skill-block-name {
  font-size: 11.5px;
  color: var(--gray-600, #606266);
  font-family: var(--font-mono, monospace);
}

.skill-block-desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--gray-700, #303133);
  line-height: 1.55;
}

.invoke-form {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  flex-wrap: wrap;
  padding-top: 6px;
  border-top: 1px dashed var(--color-border-light, #EEF0F4);
}

.invoke-result {
  margin-top: 6px;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 11.5px;

  &.result-ok {
    background: rgba(47, 158, 68, 0.06);
    border: 1px solid rgba(47, 158, 68, 0.20);
  }
  &.result-bad {
    background: rgba(229, 72, 77, 0.06);
    border: 1px solid rgba(229, 72, 77, 0.20);
  }
}

.result-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.result-status {
  font-weight: 600;
}

.result-effect,
.result-duration,
.result-record {
  font-size: 10.5px;
  color: var(--gray-500, #909399);
  font-family: var(--font-mono, monospace);
}

.result-json {
  margin: 6px 0 0;
  padding: 8px 10px;
  background: #f6f7f9;
  border-radius: 4px;
  font-family: var(--font-mono, monospace);
  font-size: 10.5px;
  color: var(--gray-700, #303133);
  overflow-x: auto;
  max-height: 180px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.result-error {
  margin: 4px 0 0;
  color: #e5484d;
  word-break: break-all;
}

.rec-agent,
.rec-skill {
  font-size: 11.5px;
  font-family: var(--font-mono, monospace);
}

.rec-agent { color: var(--gray-600, #606266); }
.rec-skill { color: var(--brand-600, #5B58E8); }

.text-muted { color: var(--color-text-secondary, #909399); }
.gate-ok, .ok { color: #2f9e44; }
.gate-bad, .bad { color: #e5484d; }
.warn { color: #e8a33d; }
</style>
